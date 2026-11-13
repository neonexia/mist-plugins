"""The refreshing credential provider (docs/design/sdk.md sections 3.2 and 3.3).

STS is an httpx.MockTransport passed through the public `http=` argument. Each test picks the Expiration the mock
answers with, relative to now, so no test waits on the clock.
"""
import json
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from mist.credentials import Credentials, Provider, Timings, scope_policy
from mist.errors import AccessDenied, IdentityError, MistError
from mist.identity import PrincipalKey, Token, TokenFile

ENDPOINT = "https://mist.test:9443"
NS = "https://sts.amazonaws.com/doc/2011-06-15/"


def iso(seconds_from_now):
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ok_xml(n, expires_in, action="AssumeRoleWithWebIdentity"):
    return (
        f'<{action}Response xmlns="{NS}"><{action}Result><Credentials>'
        f"<AccessKeyId>AK{n}</AccessKeyId><SecretAccessKey>SK{n}</SecretAccessKey>"
        f"<SessionToken>ST{n}</SessionToken><Expiration>{iso(expires_in)}</Expiration></Credentials>"
        f"</{action}Result><ResponseMetadata><RequestId>req-{n}</RequestId></ResponseMetadata></{action}Response>"
    )


def error_xml(code, message):
    return (
        f'<ErrorResponse xmlns="{NS}"><Error><Type>Sender</Type><Code>{code}</Code>'
        f"<Message>{message}</Message></Error><RequestId>req-err</RequestId></ErrorResponse>"
    )


class FakeSts:
    """Answers each STS request with the next scripted reply: an int is a success expiring that many seconds from
    now, a tuple is (status, xml)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.requests.append(request)
            n = len(self.requests)
            step = self.script[min(n, len(self.script)) - 1]
        if isinstance(step, tuple):
            return httpx.Response(step[0], text=step[1])
        action = self.form(n - 1)["Action"]
        return httpx.Response(200, text=ok_xml(n, step, action))

    def form(self, i):
        return {k: v[0] for k, v in parse_qs(self.requests[i].content.decode()).items()}


def provider(sts, identity=None, role=None, policy=None):
    return Provider(
        endpoint=ENDPOINT,
        identity=identity or Token("jwt-1"),
        role=role,
        policy=policy,
        http=httpx.Client(transport=httpx.MockTransport(sts)),
        timings=Timings(),
    )


def test_web_identity_mint_sends_the_form():
    sts = FakeSts(900)
    creds = provider(sts).get()
    assert isinstance(creds, Credentials)
    assert (creds.access_key_id, creds.secret_access_key, creds.session_token) == ("AK1", "SK1", "ST1")
    assert creds.expiration.tzinfo is not None
    request = sts.requests[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT + "/"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert sts.form(0) == {
        "Action": "AssumeRoleWithWebIdentity",
        "Version": "2011-06-15",
        "RoleSessionName": "mist-sdk",
        "RoleArn": "arn:aws:iam::000000000000:role/mist",
        "WebIdentityToken": "jwt-1",
    }
    provider(sts, role="analyst").get()
    assert sts.form(1)["RoleArn"] == "arn:aws:iam::000000000000:role/analyst"


def test_principal_key_mint_is_signed_for_sts():
    sts = FakeSts(900)
    creds = provider(sts, identity=PrincipalKey("mk-1", "s3cret")).get()
    assert creds.access_key_id == "AK1"
    auth = sts.requests[0].headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=mk-1/")
    assert auth.split("Credential=")[1].split(",")[0].endswith("/us-east-1/sts/aws4_request")
    signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
    assert "host" in signed
    assert "x-amz-date" in signed
    form = sts.form(0)
    assert form["Action"] == "AssumeRole"
    assert "WebIdentityToken" not in form
    assert "s3cret" not in sts.requests[0].content.decode()


def test_fresh_credentials_are_reused():
    sts = FakeSts(900)
    p = provider(sts)
    assert p.get() is p.get()
    assert len(sts.requests) == 1


def test_inside_300s_the_next_call_refreshes():
    sts = FakeSts(200, 900)
    p = provider(sts)
    assert p.get().access_key_id == "AK1"
    assert p.get().access_key_id == "AK2"
    assert len(sts.requests) == 2


def test_refresh_sends_the_same_role_and_policy():
    policy = scope_policy(read=["datasets/shared/"])
    sts = FakeSts(900, 900)
    p = provider(sts, role="analyst", policy=policy)
    p.get()
    policy["Statement"].clear()  # a caller changing its dict later cannot widen the next mint
    p.refresh()
    first, second = sts.form(0), sts.form(1)
    assert second["RoleArn"] == first["RoleArn"] == "arn:aws:iam::000000000000:role/analyst"
    assert second["Policy"] == first["Policy"]
    assert json.loads(second["Policy"]) == scope_policy(read=["datasets/shared/"])


def test_refresh_always_mints():
    sts = FakeSts(900, 900)
    p = provider(sts)
    p.get()
    assert p.refresh().access_key_id == "AK2"
    assert p.get().access_key_id == "AK2"


def test_a_failed_refresh_inside_300s_keeps_the_current_credentials():
    sts = FakeSts(200, (500, error_xml("InternalFailure", "boom")))
    p = provider(sts)
    first = p.get()
    assert p.get() is first
    assert len(sts.requests) == 2


def test_inside_120s_a_failed_mint_raises():
    sts = FakeSts(60, (500, error_xml("InternalFailure", "boom")))
    p = provider(sts)
    p.get()
    with pytest.raises(MistError) as e:
        p.get()
    assert e.value.status == 500


def test_a_missing_token_file_at_refresh_keeps_unexpired_credentials(tmp_path):
    path = tmp_path / "token"
    path.write_text("jwt-file\n", encoding="utf-8")
    sts = FakeSts(200, 900)
    p = provider(sts, identity=TokenFile(path))
    first = p.get()
    assert sts.form(0)["WebIdentityToken"] == "jwt-file"
    path.unlink()
    assert p.get() is first
    assert len(sts.requests) == 1
    with pytest.raises(IdentityError):
        p.refresh()


def test_threads_share_one_mint():
    sts = FakeSts(900)
    start = threading.Barrier(8)
    passed = threading.Semaphore(0)

    def held_mint(request):
        # Hold the first mint until all 8 callers are past the barrier and on their way into get(). Later mints
        # (only a provider without the re-check under its lock makes any) answer at once.
        if not sts.requests:
            for _ in range(8):
                assert passed.acquire(timeout=0.5), "the callers never got past the barrier"
        return sts(request)

    p = provider(held_mint)
    results, errors = [], []

    def worker():
        try:
            start.wait(timeout=0.5)
            passed.release()
            results.append(p.get())
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=1)
    assert not errors
    assert len(results) == 8
    assert len(sts.requests) == 1
    assert all(r is results[0] for r in results)


def test_inside_300s_a_caller_does_not_wait_for_another_callers_mint():
    sts = FakeSts(200, 900)
    entered, release = threading.Event(), threading.Event()

    def held_second_mint(request):
        if sts.requests:
            entered.set()
            assert release.wait(timeout=0.5)
        return sts(request)

    p = provider(held_second_mint)
    first = p.get()
    refreshed = []
    minter = threading.Thread(target=lambda: refreshed.append(p.get()))
    minter.start()
    try:
        assert entered.wait(timeout=0.5)
        assert p.get() is first  # the lock is held by the minting caller: no wait, current credentials
    finally:
        release.set()
        minter.join(timeout=1)
    assert refreshed[0].access_key_id == "AK2"
    assert p.get() is refreshed[0]


def test_a_network_error_becomes_mist_error_without_the_token():
    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(MistError) as e:
        provider(unreachable, identity=Token("eyJ.secret-token")).get()
    assert e.value.error == "NetworkError"
    assert e.value.status == 0
    assert "ConnectError" in e.value.message
    assert "mist.test:9443" in e.value.message
    assert e.value.__cause__ is None
    assert e.value.__context__ is None
    assert "secret-token" not in str(e.value)
    assert "secret-token" not in repr(e.value)


def network_error_for(endpoint):
    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    p = Provider(
        endpoint=endpoint,
        identity=Token("jwt-1"),
        role=None,
        policy=None,
        http=httpx.Client(transport=httpx.MockTransport(unreachable)),
        timings=Timings(),
    )
    with pytest.raises(MistError) as e:
        p.get()
    return e.value


def test_a_network_error_names_no_userinfo_from_the_endpoint():
    e = network_error_for("https://user:hunter2@mist.test:9443")
    assert "could not reach STS at https://mist.test:9443" in e.message
    assert "hunter2" not in str(e)
    assert "user" not in e.message


def test_a_network_error_keeps_ipv6_brackets():
    assert "could not reach STS at http://[::1]:8000" in network_error_for("http://[::1]:8000/").message


def test_a_network_error_inside_300s_keeps_the_current_credentials():
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, text=ok_xml(1, 200))
        raise httpx.ReadTimeout("timed out", request=request)

    p = provider(flaky)
    first = p.get()
    assert p.get() is first
    assert len(calls) == 2


def test_a_non_xml_sts_error_is_invalid_response():
    sts = FakeSts((502, "<html>" + "x" * 500))
    with pytest.raises(MistError) as e:
        provider(sts).get()
    assert e.value.status == 502
    assert e.value.error == "InvalidResponse"
    assert len(e.value.message) <= 200


def test_credentials_that_already_look_expired_are_returned_once():
    sts = FakeSts(-60, -60)
    p = provider(sts)
    assert p.get().access_key_id == "AK1"
    assert len(sts.requests) == 1
    assert p.get().access_key_id == "AK2"
    assert len(sts.requests) == 2


def test_sts_error_becomes_mist_error():
    sts = FakeSts((403, error_xml("AccessDenied", "Access denied")))
    with pytest.raises(AccessDenied) as e:
        provider(sts).get()
    assert e.value.status == 403
    assert e.value.error == "AccessDenied"
    assert e.value.message == "Access denied"
    assert "jwt-1" not in str(e.value)


def test_secrets_stay_out_of_the_credentials_repr():
    creds = provider(FakeSts(900)).get()
    assert "SK1" not in repr(creds)
    assert "ST1" not in repr(creds)


def test_scope_policy_read_prefix():
    assert scope_policy(read=["datasets/shared/"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::datasets/shared/*"},
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": "arn:aws:s3:::datasets",
                "Condition": {"StringLike": {"s3:prefix": "shared/*"}},
            },
        ],
    }


def test_scope_policy_whole_bucket_has_no_condition():
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::datasets/*"},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::datasets"},
        ],
    }
    assert scope_policy(read=["datasets"]) == expected
    assert scope_policy(read=["datasets/"]) == expected


def test_scope_policy_write_and_delete():
    assert scope_policy(write=["out/run-1/"], delete=["tmp"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": "arn:aws:s3:::out/run-1/*"},
            {"Effect": "Allow", "Action": ["s3:DeleteObject"], "Resource": "arn:aws:s3:::tmp/*"},
        ],
    }


def test_scope_is_sent_as_policy():
    scope = scope_policy(read=["datasets/shared/"])
    sts = FakeSts(900)
    provider(sts, policy=scope).get()
    sent = sts.form(0)["Policy"]
    assert sent == json.dumps(scope, separators=(",", ":"))


def test_a_non_transport_request_error_becomes_mist_error_without_the_token():
    def broken(request):
        raise httpx.DecodingError("bad gzip")

    with pytest.raises(MistError) as e:
        provider(broken, identity=Token("eyJ.secret-token")).get()
    assert e.value.error == "NetworkError" and "DecodingError" in e.value.message
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert "secret-token" not in str(e.value) + repr(e.value)
