"""The HTTP transport: auth, errors and retries (docs/design/sdk.md section 5.3, decision P8).

Every test goes through the public `Client` with an httpx.MockTransport passed as `http_client=`. Backoff is zero and
`Retry-After` is 0 where a wait would happen, so nothing sleeps.
"""
import httpx
import pytest

import mist
from mist import AccessDenied, Callback, Client, Conflict, CursorExpired, MistError, NotFound, PrincipalKey, Timings
from mist import Token, Unavailable

ENDPOINT = "https://gw:9200"
FAST = Timings(backoff_min=0, backoff_max=0)


class Mock:
    """Answers each request with the next scripted reply (an httpx.Response, or an exception to raise)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def client(self, identity=None, timings=FAST, endpoint=ENDPOINT):
        http = httpx.Client(transport=httpx.MockTransport(self))
        return Client(endpoint, identity=identity or Token("tok-1"), timings=timings, http_client=http)


def ok(body=None):
    return httpx.Response(200, json={} if body is None else body)


def err(status, error="X", message="m", headers=None, **extra):
    return httpx.Response(status, json={"error": error, "message": message, **extra}, headers=headers)


def test_bearer_token_is_read_per_attempt():
    tokens = iter(["tok-1", "tok-2"])
    m = Mock(err(401, "Unauthenticated"), ok())
    m.client(identity=Callback(lambda: next(tokens))).whoami()
    assert [r.headers["authorization"] for r in m.requests] == ["Bearer tok-1", "Bearer tok-2"]


def test_a_second_401_raises():
    m = Mock(err(401, "Unauthenticated", "no"))
    with pytest.raises(MistError) as e:
        m.client().whoami()
    assert e.value.status == 401 and len(m.requests) == 2


def test_principal_key_signs_for_service_mist():
    m = Mock(ok())
    m.client(identity=PrincipalKey("AKID", "secret")).whoami()
    auth = m.requests[0].headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "/us-east-1/mist/aws4_request" in auth


def test_principal_key_signs_a_body_and_query():
    m = Mock(ok(), ok())
    c = m.client(identity=PrincipalKey("AKID", "secret"))
    c.buckets.create("b")
    c.grants.list(bucket="a b")
    assert all("/mist/aws4_request" in r.headers["authorization"] for r in m.requests)
    assert m.requests[1].url.query == b"bucket=a%20b"


def test_error_fields():
    m = Mock(err(403, "AccessDenied", "m", rule="not_owner", request_id="r1"))
    with pytest.raises(AccessDenied) as e:
        m.client().whoami()
    got = e.value
    assert (got.status, got.error, got.message, got.rule, got.request_id) == (403, "AccessDenied", "m", "not_owner", "r1")


def test_subclasses():
    cases = [
        (err(404, "NotFound"), NotFound),
        (err(409, "BucketExists", rule="x"), Conflict),
        (err(410, "CursorExpired", oldest_cursor="o", now_cursor="n"), CursorExpired),
        (err(503, "Unavailable"), Unavailable),
    ]
    for reply, cls in cases:
        with pytest.raises(cls) as e:
            Mock(reply).client().buckets.get("b")
        if cls is CursorExpired:
            assert (e.value.oldest_cursor, e.value.now_cursor) == ("o", "n")


def test_non_json_error_body_is_invalid_response():
    html = "<html>" + "x" * 500 + "</html>"
    with pytest.raises(MistError) as e:
        Mock(httpx.Response(502, text=html)).client().buckets.create("b")
    assert (e.value.status, e.value.error, len(e.value.message)) == (502, "InvalidResponse", 200)
    with pytest.raises(MistError) as e:
        Mock(httpx.Response(500)).client().buckets.create("b")
    assert (e.value.status, e.value.error) == (500, "InvalidResponse")
    with pytest.raises(MistError) as e:  # JSON, but not the error shape
        Mock(httpx.Response(400, json={"oops": 1})).client().buckets.create("b")
    assert e.value.error == "InvalidResponse"


def test_503_honours_retry_after_then_succeeds():
    m = Mock(err(503, "Unavailable", headers={"Retry-After": "0"}), ok({"principal": "p"}))
    assert m.client().whoami() == {"principal": "p"}
    assert len(m.requests) == 2


def test_503_past_the_budget_raises_unavailable():
    m = Mock(err(503, "Unavailable", headers={"Retry-After": "1"}))
    with pytest.raises(Unavailable):
        m.client(timings=Timings(backoff_min=0, backoff_max=0, unavailable_budget=0)).whoami()
    assert len(m.requests) == 1


def test_retry_after_too_long_raises_at_once():
    m = Mock(err(503, "Unavailable", headers={"Retry-After": "3600"}))
    with pytest.raises(Unavailable):
        m.client().whoami()
    assert len(m.requests) == 1


def test_retry_after_missing_or_not_a_number_is_a_plain_5xx():
    for headers in ({}, {"Retry-After": ""}, {"Retry-After": "soon"}):
        m = Mock(err(503, "Unavailable", headers=headers), err(503, "Unavailable", headers=headers), ok())
        m.client().whoami()
        assert len(m.requests) == 3


def test_unsafe_post_is_not_retried_on_503_with_retry_after():
    m = Mock(err(503, "Unavailable", headers={"Retry-After": "0"}), ok())
    with pytest.raises(Unavailable):
        m.client().grants.create({"principal": {"issuer": "i", "id": "a"}}, "b", ["read"])
    assert len(m.requests) == 1


def test_get_is_retried_on_500_three_times():
    m = Mock(err(500, "Internal"))
    with pytest.raises(MistError) as e:
        m.client().whoami()
    assert e.value.status == 500 and len(m.requests) == 3


def test_put_and_delete_are_retried():
    m = Mock(err(500), ok())
    mist_client = m.client()
    mist_client.buckets.set_quota("b", 5)
    m2 = Mock(err(502), ok())
    m2.client().buckets.delete("b")
    assert len(m.requests) == 2 and len(m2.requests) == 2


def test_post_is_not_retried_on_500():
    m = Mock(err(500, "Internal"))
    with pytest.raises(MistError):
        m.client().buckets.create("b")
    assert len(m.requests) == 1


def test_repeatable_post_is_retried():
    m = Mock(err(500), ok())
    m.client().events.subscribe("b", name="n")
    assert len(m.requests) == 2
    m = Mock(err(500), ok())
    with pytest.raises(MistError):
        m.client().events.subscribe("b")  # unnamed: not idempotent
    assert len(m.requests) == 1


def test_network_error_is_retried_for_get_and_is_token_safe():
    boom = httpx.ConnectError("boom")
    m = Mock(boom)
    with pytest.raises(MistError) as e:
        m.client(identity=Token("SECRET-TOKEN-VALUE")).whoami()
    got = e.value
    assert len(m.requests) == 3
    assert (got.status, got.error) == (0, "NetworkError")
    assert got.message == "ConnectError: could not reach https://gw:9200"
    assert "SECRET-TOKEN-VALUE" not in str(got) + repr(got) + repr(got.args)
    assert got.__cause__ is None and got.__context__ is None


def test_network_error_on_unsafe_post_is_not_retried():
    m = Mock(httpx.ReadTimeout("slow"))
    with pytest.raises(MistError) as e:
        m.client(identity=Token("SECRET-TOKEN-VALUE")).buckets.create("b")
    assert len(m.requests) == 1 and e.value.__context__ is None and e.value.error == "NetworkError"


def test_endpoint_is_cut_to_its_origin():
    for endpoint in ("https://gw:9200/", "https://gw:9200/mist/v1", "https://user:pw@gw:9200/x?y#z"):
        m = Mock(ok())
        m.client(endpoint=endpoint).whoami()
        assert str(m.requests[0].url) == "https://gw:9200/mist/v1/whoami"


def test_endpoint_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MIST_ENDPOINT", "https://env:1/p")
    m = Mock(ok())
    Client(identity=Token("t"), http_client=httpx.Client(transport=httpx.MockTransport(m))).whoami()
    assert str(m.requests[0].url) == "https://env:1/mist/v1/whoami"
    monkeypatch.delenv("MIST_ENDPOINT")
    with pytest.raises(ValueError):
        Client(identity=Token("t"))


def test_events_config_calls_get_120s():
    m = Mock(ok())
    c = m.client()
    c.buckets.set_events("b", True)
    c.buckets.get("b")
    c.events.delete("s")
    assert [r.extensions["timeout"]["read"] for r in m.requests] == [120, 30, 120]


def test_path_values_are_quoted():
    m = Mock(ok())
    m.client().buckets.get("a/b c")
    assert m.requests[0].url.raw_path == b"/mist/v1/buckets/a%2Fb%20c"


def test_package_exports_the_public_names():
    assert mist.Client is Client and mist.MistError is MistError


def test_a_non_transport_request_error_is_a_network_error_without_the_token():
    m = Mock(httpx.DecodingError("bad gzip"))
    with pytest.raises(MistError) as e:
        m.client(identity=Token("SECRET-TOKEN-VALUE")).whoami()
    assert (e.value.status, e.value.error) == (0, "NetworkError")
    assert "DecodingError" in e.value.message
    assert "SECRET-TOKEN-VALUE" not in str(e.value) + repr(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_a_bad_endpoint_port_is_a_value_error():
    with pytest.raises(ValueError):
        Client("http://host:abc", identity=Token("t"))
