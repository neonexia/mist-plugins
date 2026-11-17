"""The typed client: every operation has a method, bodies and queries are what the spec says (sections 3.2, 5.1)."""
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from mist import Client, Timings, Token, scope_policy
from mist._generated.operations import OPERATIONS

ENDPOINT = "https://gw:9200"
NS = "https://sts.amazonaws.com/doc/2011-06-15/"
STS_OK = (
    f'<AssumeRoleWithWebIdentityResponse xmlns="{NS}"><AssumeRoleWithWebIdentityResult><Credentials>'
    "<AccessKeyId>AK</AccessKeyId><SecretAccessKey>SK</SecretAccessKey><SessionToken>ST</SessionToken>"
    "<Expiration>2999-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleWithWebIdentityResult>"
    "</AssumeRoleWithWebIdentityResponse>"
)


class Gateway:
    """Records requests; answers STS posts with credentials and every Mist call with 200 {}."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/mist/v1/sts":
            return httpx.Response(200, text=STS_OK)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": [], "next_cursor": "c"})
        return httpx.Response(200, json={})

    def client(self, **kw):
        http = httpx.Client(transport=httpx.MockTransport(self))
        return Client(ENDPOINT, identity=Token("tok"), http_client=http, **kw)

    @property
    def last(self):
        return self.requests[-1]

    def body(self):
        return json.loads(self.last.content)

    def query(self):
        return parse_qs(self.last.url.query.decode())

    def mints(self):
        return [parse_qs(r.content.decode()) for r in self.requests if r.url.path == "/mist/v1/sts"]


GRANTEE = {"principal": {"issuer": "i", "id": "a"}}


def test_every_operation_has_a_method():
    g = Gateway()
    c = g.client()
    calls = [
        lambda: c.whoami(),
        lambda: c.buckets.create("b"),
        lambda: c.buckets.get("b"),
        lambda: c.buckets.delete("b"),
        lambda: c.buckets.set_versioning("b", "enabled"),
        lambda: c.buckets.set_encryption("b", "sse-s3"),
        lambda: c.buckets.set_quota("b", 1),
        lambda: c.buckets.set_expiry("b", "p/", 7),
        lambda: c.buckets.remove_expiry("b", "p/"),
        lambda: c.buckets.set_events("b", True),
        lambda: c.grants.create(GRANTEE, "b", ["read"]),
        lambda: c.grants.list(),
        lambda: c.grants.delete("g"),
        lambda: c.events.subscribe("b"),
        lambda: c.events.list(),
        lambda: c.events.get("s"),
        lambda: c.events.delete("s"),
        lambda: c.events.rotate_secret("s"),
        lambda: c.events.resume("s"),
        lambda: c.events.deliveries("s"),
        lambda: c.events.redeliver("s", "d"),
        lambda: c.events.wait("s"),
        lambda: c.roles.create("r", [], []),
        lambda: c.roles.replace("r", [], []),
        lambda: c.roles.get("r"),
        lambda: c.roles.delete("r"),
        lambda: c.roles.assumable(),
        lambda: c.workspaces.create("b", 1),
        lambda: c.workspaces.get("b", "r"),
        lambda: c.workspaces.list(),
        lambda: c.workspaces.delete("b", "r"),
    ]
    for call in calls:
        call()
    seen = set()
    for r in g.requests:
        for op in OPERATIONS.values():
            template = op.path.split("{")[0]
            if op.method == r.method and r.url.path.startswith(template) and _matches(op, r.url.path):
                seen.add(op.id)
    # health, discovery and sts have no client method: they are open endpoints and an AWS STS form (use an AWS SDK).
    assert seen == set(OPERATIONS) - {"health", "discovery", "sts"}


def _matches(op, path):
    parts, want = path.split("/"), op.path.split("/")
    return len(parts) == len(want) and all(w.startswith("{") or w == p for w, p in zip(want, parts))


def test_buckets_create_sends_the_body():
    g = Gateway()
    c = g.client()
    c.buckets.create("b")
    assert g.body() == {"name": "b"}
    c.buckets.create("b", expiry=[{"prefix": "", "days": 7}])
    assert g.body() == {"name": "b", "expiry": [{"prefix": "", "days": 7}]}
    c.buckets.create("b", encryption="sse-s3")
    assert g.body() == {"name": "b", "encryption": "sse-s3"}


def test_set_encryption_sends_the_word():
    g = Gateway()
    g.client().buckets.set_encryption("b", "none")
    assert (g.last.method, g.last.url.path, g.body()) == ("PUT", "/mist/v1/buckets/b/encryption", {"encryption": "none"})


def test_set_quota_none_sends_null():
    g = Gateway()
    g.client().buckets.set_quota("b", None)
    assert g.body() == {"bytes": None}


def test_optional_arguments_are_left_out():
    g = Gateway()
    c = g.client()
    c.buckets.set_versioning("b", "suspended")
    assert g.body() == {"status": "suspended"}
    c.grants.create(GRANTEE, "b", ["read"], prefix="p/")
    assert g.body() == {"grantee": GRANTEE, "bucket": "b", "actions": ["read"], "prefix": "p/"}
    c.roles.create("r", [], [], description="d")
    assert g.body() == {"name": "r", "rights": [], "trust": [], "description": "d"}
    c.roles.replace("r", [], [])
    assert g.body() == {"rights": [], "trust": []} and g.last.url.path == "/mist/v1/roles/r"
    c.events.subscribe("b", "p/", name="n", delivery={"type": "feed"})
    assert g.body() == {"bucket": "b", "prefix": "p/", "name": "n", "delivery": {"type": "feed"}}


def test_grants_list_defaults_to_me():
    g = Gateway()
    c = g.client()
    c.grants.list()
    assert g.query() == {"grantee": ["me"]}
    c.grants.list(bucket="b")
    assert g.query() == {"bucket": ["b"]}


def test_remove_expiry_sends_the_prefix_as_a_query():
    g = Gateway()
    g.client().buckets.remove_expiry("b", "logs/")
    assert (g.last.method, g.last.url.path, g.query()) == ("DELETE", "/mist/v1/buckets/b/expiry", {"prefix": ["logs/"]})
    assert not g.last.content


def test_roles_assumable_and_whoami_role():
    g = Gateway()
    c = g.client()
    c.roles.assumable()
    assert g.query() == {"assumable": ["me"]}
    c.whoami(role="analyst")
    assert g.query() == {"role": ["analyst"]}
    c.whoami()
    assert g.last.url.query == b""


def test_redeliver_path():
    g = Gateway()
    g.client().events.redeliver("s1", "d1")
    assert (g.last.method, g.last.url.path) == ("POST", "/mist/v1/subscriptions/s1/deliveries/d1/redeliver")


def test_with_scope_returns_a_new_client_that_mints_with_policy():
    g = Gateway()
    m = g.client(role="analyst")
    scoped = m.with_scope(read=["datasets/shared/"])
    assert scoped is not m
    scoped.credentials()
    form = g.mints()[0]
    assert json.loads(form["Policy"][0]) == scope_policy(read=["datasets/shared/"])
    assert form["RoleArn"] == ["arn:aws:iam::000000000000:role/analyst"]
    m.credentials()
    assert "Policy" not in g.mints()[1]
    # Mist HTTP calls keep the full identity.
    scoped.whoami()
    assert g.last.headers["authorization"] == "Bearer tok"


def test_with_scope_on_a_scoped_client_raises():
    g = Gateway()
    m = g.client()
    with pytest.raises(ValueError):
        m.with_scope(read=["a"]).with_scope(read=["b"])
    with pytest.raises(ValueError):
        g.client(policy={"Version": "2012-10-17", "Statement": []}).with_scope(read=["a"])


def test_with_scope_argument_errors():
    m = Gateway().client()
    with pytest.raises(ValueError):
        m.with_scope()
    with pytest.raises(ValueError):
        m.with_scope(read=["a"], policy={"Statement": []})


def test_with_scope_copies_the_policy():
    g = Gateway()
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}]}
    scoped = g.client().with_scope(policy=policy)
    policy["Statement"].append({"Effect": "Allow", "Action": ["s3:*"], "Resource": "*"})
    scoped.credentials()
    sent = json.loads(g.mints()[0]["Policy"][0])
    assert len(sent["Statement"]) == 1
    caller = {"Statement": [1]}
    c = g.client(policy=caller)
    caller["Statement"].append(2)
    c.credentials()
    assert json.loads(g.mints()[1]["Policy"][0]) == {"Statement": [1]}


def test_credentials_and_refresh():
    g = Gateway()
    c = g.client()
    assert c.credentials().access_key_id == "AK"
    c.credentials()
    assert len(g.mints()) == 1
    c.refresh()
    assert len(g.mints()) == 2


def test_client_records_its_identity_source():
    t = Token("tok")
    c = Client(ENDPOINT, identity=t, http_client=httpx.Client(transport=httpx.MockTransport(Gateway())))
    assert c.identity_source is t


def test_ca_bundle_reaches_httpx(tmp_path):
    # A client built with ca_bundle= and no http_client reads that file at once: a missing one fails here, and a
    # file with no certificate in it is refused by the TLS context.
    with pytest.raises(FileNotFoundError):
        Client(ENDPOINT, identity=Token("t"), ca_bundle=str(tmp_path / "nope.pem"))
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(Exception):
        Client(ENDPOINT, identity=Token("t"), ca_bundle=str(junk))
    import certifi

    c = Client(ENDPOINT, identity=Token("t"), ca_bundle=certifi.where())
    c.close()


def test_workspaces_send_the_spec_shapes():
    g = Gateway()
    c = g.client()
    c.workspaces.create("b", 7)
    assert g.last.url.path == "/mist/v1/workspaces" and g.last.method == "POST"
    body = g.body()
    first = body.pop("run_id")
    assert re.fullmatch(r"run-[0-9a-f]{26}", first)
    assert body == {"bucket": "b", "ttl_days": 7}
    c.workspaces.create("b", 7)
    assert g.body()["run_id"] != first
    c.workspaces.create("b", 7, run_id="r1", grantee=GRANTEE, actions=["read"])
    assert g.body() == {"bucket": "b", "ttl_days": 7, "run_id": "r1", "grant": {"grantee": GRANTEE, "actions": ["read"]}}
    c.workspaces.create("b", 1, grantee=GRANTEE)
    assert g.body()["grant"] == {"grantee": GRANTEE}
    with pytest.raises(ValueError):
        c.workspaces.create("b", 1, actions=["read"])
    c.workspaces.get("b", "r1")
    assert (g.last.method, g.last.url.path) == ("GET", "/mist/v1/workspaces/b/r1")
    c.workspaces.list()
    assert (g.last.method, g.last.url.path, g.last.url.query) == ("GET", "/mist/v1/workspaces", b"")
    c.workspaces.delete("b", "r1")
    assert (g.last.method, g.last.url.path) == ("DELETE", "/mist/v1/workspaces/b/r1")
