"""mist.s3(): a stock boto3 client over the refreshing provider, one re-mint on 403 (sections 3.3 and 4, decision P5)."""
from urllib.parse import parse_qs

import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from mist import Client, Token

NS = "https://sts.amazonaws.com/doc/2011-06-15/"


def sts_reply(n):
    return (
        f'<AssumeRoleWithWebIdentityResponse xmlns="{NS}"><AssumeRoleWithWebIdentityResult><Credentials>'
        f"<AccessKeyId>AK{n}</AccessKeyId><SecretAccessKey>SK{n}</SecretAccessKey><SessionToken>ST{n}</SessionToken>"
        "<Expiration>2999-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleWithWebIdentityResult>"
        "</AssumeRoleWithWebIdentityResponse>"
    )


class Sts:
    """Answers each mint with AK1, AK2, ...; `fail_from` makes later mints answer 500."""

    def __init__(self, fail_from=None):
        self.forms = []
        self.fail_from = fail_from

    def __call__(self, request):
        self.forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if self.fail_from and len(self.forms) >= self.fail_from:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=sts_reply(len(self.forms)))


def mist_client(server, sts=None, **kw):
    sts = sts or Sts()
    http = httpx.Client(transport=httpx.MockTransport(sts))
    return Client("https://gw:9200", s3_endpoint=server.url, identity=Token("tok"), http_client=http, **kw), sts


def test_s3_client_signs_with_minted_credentials(s3server):
    srv = s3server()
    m, sts = mist_client(srv)
    assert m.s3().get_object(Bucket="b", Key="k")["Body"].read() == b"hello"
    assert srv.keys == ["AK1"]
    assert len(sts.forms) == 1


def test_403_access_denied_remints_and_retries_once(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv)
    assert m.s3().get_object(Bucket="b", Key="k")["Body"].read() == b"hello"
    assert srv.keys == ["AK1", "AK2"]
    assert len(sts.forms) == 2


def test_the_403_remint_keeps_role_and_policy(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv, role="analyst")
    scoped = m.with_scope(read=["datasets/shared/"])
    scoped.s3().get_object(Bucket="datasets", Key="shared/k")
    assert len(sts.forms) == 2
    assert sts.forms[0]["RoleArn"] == sts.forms[1]["RoleArn"]
    assert sts.forms[0]["RoleArn"].endswith("role/analyst")
    assert sts.forms[0]["Policy"] == sts.forms[1]["Policy"]


def test_a_second_403_raises_client_error(s3server):
    srv = s3server(deny=["AK1", "AK2"])
    m, sts = mist_client(srv)
    with pytest.raises(ClientError) as e:
        m.s3().get_object(Bucket="b", Key="k")
    assert e.value.response["Error"]["Code"] == "AccessDenied"
    assert len(srv.seen) == 2
    assert len(sts.forms) == 2


def test_a_failed_remint_surfaces_the_original_403(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv, sts=Sts(fail_from=2))
    with pytest.raises(ClientError) as e:
        m.s3().get_object(Bucket="b", Key="k")
    assert e.value.response["Error"]["Code"] == "AccessDenied"
    assert srv.keys == ["AK1"]
    assert "SK1" not in str(e.value) and "ST1" not in str(e.value)


def test_other_errors_are_not_reminted(s3server):
    srv = s3server()
    m, sts = mist_client(srv)
    with pytest.raises(ClientError) as e:
        m.s3().get_object(Bucket="b", Key="missing")
    assert e.value.response["Error"]["Code"] == "NoSuchKey"
    assert len(sts.forms) == 1
    assert len(srv.seen) == 1


def test_path_style_and_endpoint(s3server):
    srv = s3server()
    m, _ = mist_client(srv)
    s3 = m.s3()
    s3.get_object(Bucket="bucket", Key="a/key")
    assert srv.seen[0][1] == "/bucket/a/key"
    assert s3.meta.endpoint_url == srv.url
    assert s3.meta.region_name == "us-east-1"


def test_caller_client_kwargs_win(s3server):
    srv = s3server()
    m, _ = mist_client(srv)
    s3 = m.s3(region_name="eu-west-1", config=Config(retries={"mode": "standard", "max_attempts": 1}))
    assert s3.meta.region_name == "eu-west-1"


def test_s3_endpoint_comes_from_the_environment(s3server, monkeypatch):
    srv = s3server()
    monkeypatch.setenv("MIST_S3_ENDPOINT", srv.url)
    http = httpx.Client(transport=httpx.MockTransport(Sts()))
    m = Client("https://gw:9200", identity=Token("tok"), http_client=http)
    assert m.s3().meta.endpoint_url == srv.url


def test_missing_s3_endpoint_names_both_ways_to_set_it(monkeypatch):
    monkeypatch.delenv("MIST_S3_ENDPOINT", raising=False)
    http = httpx.Client(transport=httpx.MockTransport(Sts()))
    m = Client("https://gw:9200", identity=Token("tok"), http_client=http)
    with pytest.raises(ValueError) as e:
        m.s3()
    assert "s3_endpoint" in str(e.value) and "MIST_S3_ENDPOINT" in str(e.value)


def test_ca_bundle_becomes_verify(s3server):
    srv = s3server()
    http = httpx.Client(transport=httpx.MockTransport(Sts()))
    m = Client("https://gw:9200", s3_endpoint=srv.url, identity=Token("tok"), http_client=http, ca_bundle="/x/ca.pem")
    assert m.s3_client_kwargs()["verify"] == "/x/ca.pem"


def test_boto3_session_shares_the_credentials(s3server):
    srv = s3server()
    m, sts = mist_client(srv)
    session = m.boto3_session()
    assert m.boto3_session() is session
    frozen = session.get_credentials().get_frozen_credentials()
    assert (frozen.access_key, frozen.secret_key, frozen.token) == ("AK1", "SK1", "ST1")
    assert session.get_credentials().access_key == "AK1"
    m.refresh()
    assert session.get_credentials().get_frozen_credentials().access_key == "AK2"
    assert len(sts.forms) == 2


def test_a_library_client_from_the_session_works(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv)
    kw = m.s3_client_kwargs()
    assert set(kw) == {"endpoint_url", "region_name", "verify", "config"}
    lib = m.boto3_session().client("s3", **kw)
    lib.get_object(Bucket="bucket", Key="k")
    assert srv.keys == ["AK1", "AK2"]
    assert srv.seen[0][1] == "/bucket/k"
    assert len(sts.forms) == 2


def test_put_object_takes_if_none_match(s3server):
    srv = s3server()
    m, _ = mist_client(srv)
    m.s3().put_object(Bucket="b", Key="k", Body=b"x", IfNoneMatch="*")
    headers = {k.lower(): v for k, v in srv.seen[0][3].items()}
    assert headers["if-none-match"] == "*"


def test_head_403_without_a_body_remints_once(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv)
    m.s3().head_object(Bucket="b", Key="k")
    assert srv.keys == ["AK1", "AK2"]
    assert len(sts.forms) == 2


def test_a_later_request_can_remint_again(s3server):
    srv = s3server(deny=["AK1"])
    m, sts = mist_client(srv)
    s3 = m.s3()
    s3.get_object(Bucket="b", Key="k")
    srv.deny.add("AK2")
    s3.get_object(Bucket="b", Key="k")
    assert srv.keys == ["AK1", "AK2", "AK2", "AK3"]
    assert len(sts.forms) == 3


def test_concurrent_s3_calls_share_one_session(s3server):
    import threading

    srv = s3server()
    m, _ = mist_client(srv)
    clients, errors = [], []

    def build():
        try:
            clients.append(m.s3())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors and len(clients) == 8
