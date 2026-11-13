"""mist-mcp against a fake Mist gateway: a real local HTTP server that answers STS and the Mist HTTP API."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import Client, StdioServerParameters

from mist_mcp.server import Config, build_server, skill_topics

pytestmark = pytest.mark.anyio

NS = "https://sts.amazonaws.com/doc/2011-06-15/"
STS_OK = (
    f'<AssumeRoleWithWebIdentityResponse xmlns="{NS}"><AssumeRoleWithWebIdentityResult><Credentials>'
    "<AccessKeyId>AKFAKE</AccessKeyId><SecretAccessKey>SKFAKE</SecretAccessKey><SessionToken>STFAKE</SessionToken>"
    "<Expiration>2999-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleWithWebIdentityResult>"
    "</AssumeRoleWithWebIdentityResponse>"
)
TOOLS = {
    "whoami", "credentials", "create_bucket", "get_bucket", "delete_bucket", "create_grant", "list_grants",
    "delete_grant", "create_role", "list_roles", "delete_role", "create_subscription", "list_subscriptions",
    "delete_subscription", "feed", "create_workspace", "list_workspaces", "guide",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Gateway:
    """Records every request. Replies come from `routes`: (method, path) -> (status, json body)."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlsplit(self.path)
                gw.requests.append({
                    "method": self.command, "path": url.path, "query": parse_qs(url.query),
                    "body": body, "auth": self.headers.get("Authorization"),
                })
                if self.command == "POST" and url.path == "/":
                    self._send(200, STS_OK.encode(), "text/xml")
                    return
                status, reply = gw.routes.get((self.command, url.path), (200, {}))
                self._send(status, json.dumps(reply).encode(), "application/json")

            def _send(self, status, data, ctype):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def mist_calls(self):
        return [r for r in self.requests if r["path"] != "/"]


@pytest.fixture
def gateway():
    gw = Gateway()
    yield gw
    gw.httpd.shutdown()


@pytest.fixture
def env(gateway, tmp_path):
    token = tmp_path / "token.jwt"
    token.write_text("header.payload.sig\n")
    return {
        "MIST_ENDPOINT": gateway.url,
        "MIST_S3_ENDPOINT": "http://127.0.0.1:9",
        "MIST_IDENTITY_TOKEN_FILE": str(token),
    }


def body(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def error(result):
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


async def test_lists_the_tools_with_annotations(env):
    async with Client(build_server(Config(env))) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == TOOLS
    assert tools["whoami"].annotations.read_only_hint is True
    assert tools["delete_bucket"].annotations.destructive_hint is True
    assert tools["create_bucket"].annotations.destructive_hint is False
    assert tools["feed"].annotations.read_only_hint is False  # reading after a cursor acknowledges it


async def test_no_tool_schema_has_a_null(env):
    """LM Studio's Nemotron rejects a tool whose schema holds `"default": null`; keep every schema free of nulls."""

    def nulls(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if v is None or v == "null" or v == {"type": "null"}:
                    yield f"{path}.{k}"
                yield from nulls(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if v is None or v == {"type": "null"}:
                    yield f"{path}[{i}]"
                yield from nulls(v, f"{path}[{i}]")

    async with Client(build_server(Config(env))) as c:
        tools = (await c.list_tools()).tools
    found = [p for t in tools for p in nulls(t.input_schema, t.name)]
    assert found == []
    whoami = next(t for t in tools if t.name == "whoami").input_schema
    assert whoami["properties"]["role"]["type"] == "string"
    assert "role" not in whoami.get("required", [])


async def test_explicit_null_is_still_accepted(env, gateway):
    gateway.routes[("GET", "/mist/v1/whoami")] = (200, {"principal": {"issuer": "i", "id": "a"}})
    async with Client(build_server(Config(env))) as c:
        body(await c.call_tool("whoami", {"role": None}))
    assert gateway.mist_calls()[0]["query"] == {}


async def test_whoami_sends_the_bearer_token(env, gateway):
    gateway.routes[("GET", "/mist/v1/whoami")] = (200, {"principal": {"issuer": "i", "id": "a"}})
    async with Client(build_server(Config(env))) as c:
        reply = body(await c.call_tool("whoami", {}))
    assert reply["principal"]["id"] == "a"
    assert gateway.mist_calls()[0]["auth"] == "Bearer header.payload.sig"


async def test_credentials_mint_over_sts(env, gateway):
    async with Client(build_server(Config(env))) as c:
        reply = body(await c.call_tool("credentials", {}))
    assert reply["access_key_id"] == "AKFAKE"
    assert reply["s3_endpoint"] == "http://127.0.0.1:9"
    assert reply["env"]["AWS_SESSION_TOKEN"] == "STFAKE"
    assert reply["env"]["AWS_ENDPOINT_URL_S3"] == "http://127.0.0.1:9"
    sts = [r for r in gateway.requests if r["path"] == "/"]
    assert b"AssumeRoleWithWebIdentity" in sts[0]["body"]


async def test_credentials_with_a_role_send_the_role_arn(env, gateway):
    async with Client(build_server(Config(env))) as c:
        body(await c.call_tool("credentials", {"role": "analyst"}))
    sts = [r for r in gateway.requests if r["path"] == "/"]
    assert b"role%2Fanalyst" in sts[0]["body"]


@pytest.mark.parametrize(
    "tool,args,method,path,query,sent",
    [
        ("create_bucket", {"name": "results"}, "POST", "/mist/v1/buckets", {}, {"name": "results"}),
        ("get_bucket", {"name": "results"}, "GET", "/mist/v1/buckets/results", {}, None),
        ("delete_bucket", {"name": "results"}, "DELETE", "/mist/v1/buckets/results", {}, None),
        (
            "create_grant",
            {"grantee": {"principal": {"issuer": "i", "id": "b"}}, "bucket": "results", "actions": ["read"],
             "prefix": "run-1/"},
            "POST", "/mist/v1/grants", {},
            {"grantee": {"principal": {"issuer": "i", "id": "b"}}, "bucket": "results", "actions": ["read"],
             "prefix": "run-1/"},
        ),
        ("list_grants", {}, "GET", "/mist/v1/grants", {"grantee": ["me"]}, None),
        ("list_grants", {"bucket": "results"}, "GET", "/mist/v1/grants", {"bucket": ["results"]}, None),
        ("delete_grant", {"id": "g1"}, "DELETE", "/mist/v1/grants/g1", {}, None),
        (
            "create_role",
            {"name": "reader", "rights": [{"bucket": "results", "actions": ["read"]}],
             "trust": [{"principal": {"issuer": "i", "id": "b"}}]},
            "POST", "/mist/v1/roles", {},
            {"name": "reader", "rights": [{"bucket": "results", "actions": ["read"]}],
             "trust": [{"principal": {"issuer": "i", "id": "b"}}]},
        ),
        ("list_roles", {}, "GET", "/mist/v1/roles", {"assumable": ["me"]}, None),
        ("delete_role", {"name": "reader"}, "DELETE", "/mist/v1/roles/reader", {}, None),
        (
            "create_subscription", {"bucket": "results", "prefix": "in/", "name": "w1"},
            "POST", "/mist/v1/subscriptions", {}, {"bucket": "results", "prefix": "in/", "name": "w1"},
        ),
        ("list_subscriptions", {}, "GET", "/mist/v1/subscriptions", {}, None),
        ("delete_subscription", {"id": "s1"}, "DELETE", "/mist/v1/subscriptions/s1", {}, None),
        (
            "create_workspace", {"bucket": "results", "ttl_days": 3, "run_id": "r1"},
            "POST", "/mist/v1/workspaces", {}, {"bucket": "results", "ttl_days": 3, "run_id": "r1"},
        ),
        ("list_workspaces", {}, "GET", "/mist/v1/workspaces", {}, None),
    ],
)
async def test_each_tool_is_its_route(env, gateway, tool, args, method, path, query, sent):
    async with Client(build_server(Config(env))) as c:
        body(await c.call_tool(tool, args))
    call = gateway.mist_calls()[0]
    assert (call["method"], call["path"], call["query"]) == (method, path, query)
    if sent is not None:
        assert json.loads(call["body"]) == sent


async def test_feed_returns_events_and_cursor_and_caps_wait(env, gateway):
    gateway.routes[("GET", "/mist/v1/subscriptions/s1/events")] = (
        200, {"events": [{"id": "e1", "key": "in/a"}], "next_cursor": "c2"}
    )
    async with Client(build_server(Config(env))) as c:
        reply = body(await c.call_tool("feed", {"subscription_id": "s1", "after": "c1", "wait": 5}))
        too_long = await c.call_tool("feed", {"subscription_id": "s1", "wait": 60})
    assert reply == {"events": [{"id": "e1", "key": "in/a"}], "next_cursor": "c2"}
    assert gateway.mist_calls()[0]["query"] == {"after": ["c1"], "wait": ["5"]}
    assert too_long.is_error  # wait is bounded at 25 s by the schema


async def test_refusal_returns_error_and_rule(env, gateway):
    gateway.routes[("DELETE", "/mist/v1/buckets/theirs")] = (
        403, {"error": "AccessDenied", "message": "you do not own theirs", "rule": "not_owner"}
    )
    async with Client(build_server(Config(env))) as c:
        err = error(await c.call_tool("delete_bucket", {"name": "theirs"}))
    assert err["error"] == "AccessDenied"
    assert err["rule"] == "not_owner"
    assert err["status"] == 403


async def test_expired_cursor_returns_restart_cursors(env, gateway):
    gateway.routes[("GET", "/mist/v1/subscriptions/s1/events")] = (
        410, {"error": "CursorExpired", "message": "too old", "oldest_cursor": "o", "now_cursor": "n"}
    )
    async with Client(build_server(Config(env))) as c:
        err = error(await c.call_tool("feed", {"subscription_id": "s1", "after": "c0", "wait": 0}))
    assert (err["error"], err["oldest_cursor"], err["now_cursor"]) == ("CursorExpired", "o", "n")


async def test_unconfigured_server_starts_and_says_what_is_missing():
    async with Client(build_server(Config({}))) as c:
        assert len((await c.list_tools()).tools) == len(TOOLS)
        err = error(await c.call_tool("whoami", {}))
    assert err["error"] == "NotConfigured"
    assert "MIST_ENDPOINT" in err["message"]


async def test_missing_token_file_is_an_identity_error(env):
    env["MIST_IDENTITY_TOKEN_FILE"] = "/nonexistent/token.jwt"
    async with Client(build_server(Config(env))) as c:
        err = error(await c.call_tool("whoami", {}))
    assert err["error"] == "IdentityError"
    assert "header.payload" not in json.dumps(err)


async def test_guide_lists_and_returns_topics(env):
    topics = skill_topics()
    assert "mist-getting-started" in topics
    async with Client(build_server(Config(env))) as c:
        listing = (await c.call_tool("guide", {})).content[0].text
        text = (await c.call_tool("guide", {"topic": "getting-started"})).content[0].text
        missing = await c.call_tool("guide", {"topic": "nope"})
    assert "mist-run-workspaces" in listing
    assert text.startswith("---\nname: mist-getting-started")
    assert missing.is_error


async def test_instructions_name_every_topic(env):
    async with Client(build_server(Config(env))) as c:
        instructions = c.instructions
    for topic in skill_topics():
        assert topic in instructions


async def test_runs_over_stdio(env, gateway):
    gateway.routes[("GET", "/mist/v1/workspaces")] = (200, {"workspaces": []})
    params = StdioServerParameters(command=sys.executable, args=["-m", "mist_mcp.server"], env=env)
    async with Client(params) as c:
        reply = body(await c.call_tool("list_workspaces", {}))
    assert reply == {"workspaces": []}
