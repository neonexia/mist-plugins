"""events.wait() and events.stream(): one long-poll, then the loop over it (docs/design/sdk.md section 6)."""
import itertools
from urllib.parse import parse_qs

import httpx
import pytest

from mist import Client, CursorExpired, Gap, MistError, Timings, Token, Unavailable

ENDPOINT = "https://gw:9200"
NS = "https://sts.amazonaws.com/doc/2011-06-15/"
STS_OK = (
    f'<AssumeRoleWithWebIdentityResponse xmlns="{NS}"><AssumeRoleWithWebIdentityResult><Credentials>'
    "<AccessKeyId>AK</AccessKeyId><SecretAccessKey>SK</SecretAccessKey><SessionToken>ST</SessionToken>"
    "<Expiration>2999-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleWithWebIdentityResult>"
    "</AssumeRoleWithWebIdentityResponse>"
)


def ev(i):
    return {"id": f"e{i}", "cursor": f"c{i}"}


def batch(ids, cursor):
    return httpx.Response(200, json={"events": [ev(i) for i in ids], "next_cursor": cursor})


class Feed:
    """Answers feed reads from a script (a Response, or an exception to raise); records the feed requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.reads = []
        self.timeouts = []

    def __call__(self, request):
        if request.url.path == "/":
            return httpx.Response(200, text=STS_OK)
        self.reads.append(request)
        self.timeouts.append(request.extensions["timeout"])
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def client(self):
        http = httpx.Client(transport=httpx.MockTransport(self))
        return Client(ENDPOINT, identity=Token("tok"), http_client=http, timings=Timings(backoff_min=0, backoff_max=0))

    def query(self, n):
        return parse_qs(self.reads[n].url.query.decode())


def error(status, body, **headers):
    return httpx.Response(status, json=body, headers=headers)


def test_wait_returns_events_and_next_cursor():
    f = Feed(batch([1, 2], "c2"))
    events, cursor = f.client().events.wait("sub1")
    assert [e["id"] for e in events] == ["e1", "e2"] and cursor == "c2"
    assert f.reads[0].url.path == "/mist/v1/subscriptions/sub1/events"
    assert f.query(0) == {"wait": ["25"]}


def test_wait_sends_after_and_limit():
    f = Feed(batch([], "c9"))
    f.client().events.wait("s", after="c5", wait=3, limit=10)
    assert f.query(0) == {"after": ["c5"], "wait": ["3"], "limit": ["10"]}


def test_wait_timeout_is_wait_plus_10():
    f = Feed(batch([], "c"))
    f.client().events.wait("s", wait=7)
    assert f.timeouts[0]["read"] == 17


def test_wait_does_not_retry_a_5xx():
    f = Feed(error(500, {"error": "Internal", "message": "x"}), batch([], "c"))
    with pytest.raises(MistError):
        f.client().events.wait("s")
    assert len(f.reads) == 1


def test_stream_passes_the_cursor_forward():
    f = Feed(batch([1], "c1"), batch([2], "c2"))
    it = f.client().events.stream("s", after="c0")
    assert next(it)["id"] == "e1"
    assert len(f.reads) == 1  # nothing is acknowledged before the consumer asks for more
    assert next(it)["id"] == "e2"
    assert f.query(0)["after"] == ["c0"] and f.query(1)["after"] == ["c1"]


def test_stream_drops_a_repeated_id():
    f = Feed(batch([1, 2], "c2"), batch([2, 3], "c3"))
    it = f.client().events.stream("s")
    assert [next(it)["id"] for _ in range(3)] == ["e1", "e2", "e3"]


def test_stream_remembers_only_the_last_1000_ids():
    f = Feed(batch(range(0, 1001), "c1"), batch([0], "c2"))
    it = f.client().events.stream("s")
    got = [next(it)["id"] for _ in range(1002)]
    assert got[0] == "e0" and got[-1] == "e0"


def test_stream_retries_a_network_error():
    f = Feed(httpx.ConnectError("down"), httpx.ConnectError("down"), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"
    assert len(f.reads) == 3


def test_stream_honours_retry_after_on_503():
    f = Feed(error(503, {"error": "Unavailable", "message": "x"}, **{"Retry-After": "0"}), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"
    assert len(f.reads) == 2


def test_stream_retries_a_503_without_retry_after():
    f = Feed(error(503, {"error": "Unavailable", "message": "x"}), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_stream_retries_a_gateway_5xx(status):
    f = Feed(error(status, {"error": "Internal", "message": "x"}), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"
    assert len(f.reads) == 2


def test_stream_raises_on_403_with_rule():
    f = Feed(error(403, {"error": "AccessDenied", "message": "off", "rule": "events-disabled"}))
    with pytest.raises(MistError) as e:
        next(f.client().events.stream("s"))
    assert e.value.status == 403 and e.value.rule == "events-disabled" and len(f.reads) == 1


def test_stream_raises_on_400():
    f = Feed(error(400, {"error": "InvalidCursor", "message": "ahead"}))
    with pytest.raises(MistError) as e:
        next(f.client().events.stream("s", after="zzz"))
    assert e.value.status == 400 and len(f.reads) == 1


def test_wait_raises_unavailable_on_503():
    f = Feed(error(503, {"error": "Unavailable", "message": "x"}))
    with pytest.raises(Unavailable):
        f.client().events.wait("s")


GONE = {"error": "CursorExpired", "message": "old", "oldest_cursor": "c-old", "now_cursor": "c-now"}


def test_stream_raises_cursor_expired_by_default():
    f = Feed(error(410, GONE))
    with pytest.raises(CursorExpired) as e:
        next(f.client().events.stream("s", after="c0"))
    assert e.value.oldest_cursor == "c-old" and e.value.now_cursor == "c-now"


def test_stream_on_expired_oldest_yields_a_gap_and_continues():
    f = Feed(error(410, GONE), batch([1], "c1"))
    it = f.client().events.stream("s", after="c0", on_expired="oldest")
    assert next(it) == Gap("c-old", "oldest")
    assert next(it)["id"] == "e1"
    assert f.query(1)["after"] == ["c-old"]


def test_stream_on_expired_now():
    f = Feed(error(410, GONE), batch([1], "c1"))
    it = f.client().events.stream("s", after="c0", on_expired="now")
    assert next(it) == Gap("c-now", "now")
    next(it)
    assert f.query(1)["after"] == ["c-now"]


def test_stream_rejects_an_unknown_on_expired():
    with pytest.raises(ValueError):
        Feed().client().events.stream("s", on_expired="never")


def test_stream_retries_a_401_once():
    f = Feed(error(401, {"error": "Unauthorized", "message": "x"}), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"


def test_stream_survives_many_consecutive_failures():
    f = Feed(*[httpx.ConnectError("down")] * 1100, batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"
    assert len(f.reads) == 1101


def test_stream_caps_a_huge_retry_after():
    f = Feed(error(503, {"error": "Unavailable", "message": "x"}, **{"Retry-After": "999999999"}), batch([1], "c1"))
    assert next(f.client().events.stream("s"))["id"] == "e1"


def test_stream_raises_on_a_second_consecutive_401():
    unauth = {"error": "Unauthorized", "message": "x"}
    f = Feed(error(401, unauth), error(401, unauth))
    with pytest.raises(MistError) as e:
        next(f.client().events.stream("s"))
    assert e.value.status == 401 and len(f.reads) == 2


def test_stream_raises_on_410_without_the_chosen_cursor():
    f = Feed(error(410, {"error": "CursorExpired", "message": "old", "now_cursor": "c-now"}))
    with pytest.raises(CursorExpired):
        next(f.client().events.stream("s", on_expired="oldest"))
