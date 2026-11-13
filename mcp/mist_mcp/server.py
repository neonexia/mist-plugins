"""The Mist MCP server.

Mist's control plane as tools: identity, buckets, grants, roles, change events and run workspaces, plus
`credentials` (minted S3 keys) and `guide` (the teaching text). No object tools: data goes over S3.

Configuration comes from the environment, as for every Mist pack:
`MIST_ENDPOINT` (the gateway), `MIST_S3_ENDPOINT`, `MIST_CA_BUNDLE` (only for a private CA), and the identity:
`MIST_IDENTITY_TOKEN_FILE` (a file holding a JWT), or `MIST_PRINCIPAL_KEY_ID` with `MIST_PRINCIPAL_KEY_SECRET`.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional

import anyio
import mist
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

SKILLS_DIR = Path(__file__).parent / "skills"
FEED_MAX_WAIT = 25
REGION = "us-east-1"

INSTRUCTIONS = """\
Mist is an S3-compatible object store for agents. You act as your own identity (an agent principal). Data goes over
plain S3; these tools are the control plane only. They never read or write objects.

- Who am I, what can I reach: `whoami` (identity, buckets you own, rights from grants and roles, roles you may assume).
- Read or write data: call `credentials` for 15-minute S3 keys and the S3 endpoint, then use any S3 client
  (the AWS CLI with the returned environment variables, boto3, or the harness's S3 tools). Path-style addressing,
  region us-east-1. Call `credentials` again when they expire or after you get a new grant.
- Your own storage: `create_bucket` (you become its owner). For one task's scratch space use `create_workspace`
  (a folder `workspaces/<N>d/<run_id>/` that expires after N days, optionally shared with one other agent).
- Sharing: `create_grant` gives another principal read/write/delete on a bucket folder you own; `create_role` makes
  a reusable template of rights that trusted principals choose when they mint credentials.
- Change events: `create_subscription` on a folder, then call `feed` in a loop with the returned `next_cursor`.
  Reading with `after=<cursor>` acknowledges everything before it.
- Errors come back as JSON with `error`, `message` and, for refusals, `rule` (for example `not_owner`). Read the
  rule and correct the call; do not retry a refusal unchanged.

Call `guide` with a topic for the full how-to: mist-getting-started, mist-choosing-a-surface, mist-run-workspaces,
mist-sharing-with-grants-and-roles, mist-change-events, mist-limits-and-errors.
"""

Grantee = Annotated[
    Dict[str, Any],
    Field(
        description='Who gets the rights: {"principal": {"issuer": "<iss>", "id": "<id>"}} for one agent '
        '(see `whoami` for your own issuer and id), or {"claim": {"issuer": "<iss>", "claim": "<name>", '
        '"value": "<value>"}} for everyone whose token carries that claim value.'
    ),
]
Actions = Annotated[
    List[Literal["read", "write", "delete", "delegate"]],
    Field(description="Action classes: read, write, delete, delegate (delegate lets the grantee re-share)."),
]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CREATE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)


class Config:
    """Builds `mist.Client`s from the environment, lazily, one per role, so the server starts without config."""

    def __init__(self, env: Optional[dict] = None, http_client=None) -> None:
        self._env = os.environ if env is None else env
        self._http = http_client
        self._clients: Dict[Optional[str], mist.Client] = {}
        self._lock = threading.Lock()

    def identity(self):
        key_id = self._env.get("MIST_PRINCIPAL_KEY_ID")
        secret = self._env.get("MIST_PRINCIPAL_KEY_SECRET")
        if bool(key_id) != bool(secret):
            raise _not_configured("set both MIST_PRINCIPAL_KEY_ID and MIST_PRINCIPAL_KEY_SECRET, or neither")
        if key_id:
            return mist.PrincipalKey(key_id, secret)
        path = self._env.get("MIST_IDENTITY_TOKEN_FILE") or self._env.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        if not path:
            raise _not_configured(
                "no identity: set MIST_IDENTITY_TOKEN_FILE to a file holding the agent's JWT, "
                "or MIST_PRINCIPAL_KEY_ID and MIST_PRINCIPAL_KEY_SECRET"
            )
        return mist.TokenFile(path)

    @property
    def s3_endpoint(self) -> Optional[str]:
        return self._env.get("MIST_S3_ENDPOINT") or None

    @property
    def ca_bundle(self) -> Optional[str]:
        return self._env.get("MIST_CA_BUNDLE") or None

    def client(self, role: Optional[str] = None) -> mist.Client:
        with self._lock:
            if role not in self._clients:
                endpoint = self._env.get("MIST_ENDPOINT")
                if not endpoint:
                    raise _not_configured("no endpoint: set MIST_ENDPOINT to the Mist gateway, e.g. https://mist.example.com:9200")
                try:
                    self._clients[role] = mist.Client(
                        endpoint,
                        s3_endpoint=self.s3_endpoint,
                        identity=self.identity(),
                        role=role,
                        ca_bundle=self.ca_bundle,
                        session_name="mist-mcp",
                        http_client=self._http,
                    )
                except (ValueError, OSError) as e:
                    raise _not_configured(str(e)) from None
            return self._clients[role]


def _not_configured(message: str) -> ToolError:
    return ToolError(json.dumps({"error": "NotConfigured", "message": message}))


def _error(e: Exception) -> ToolError:
    """The tool error the model sees: Mist's `error`, `message` and `rule`, as JSON."""
    if isinstance(e, mist.MistError):
        body: Dict[str, Any] = {"status": e.status, "error": e.error, "message": e.message}
        if e.rule:
            body["rule"] = e.rule
        if e.request_id:
            body["request_id"] = e.request_id
        if isinstance(e, mist.CursorExpired):
            body["oldest_cursor"] = e.oldest_cursor
            body["now_cursor"] = e.now_cursor
        return ToolError(json.dumps(body))
    if isinstance(e, mist.IdentityError):
        return ToolError(json.dumps({"error": "IdentityError", "message": str(e)}))
    if isinstance(e, ValueError):
        return ToolError(json.dumps({"error": "InvalidArgument", "message": str(e)}))
    return ToolError(json.dumps({"error": type(e).__name__, "message": "unexpected failure"}))


def _body(**fields) -> dict:
    return {k: v for k, v in fields.items() if v is not None}


def skill_topics() -> List[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def build_server(config: Optional[Config] = None) -> MCPServer:
    config = config or Config()
    server = MCPServer(name="mist", instructions=INSTRUCTIONS, version="0.1.0")

    async def run(fn: Callable[[], Any]) -> Any:
        def call():
            try:
                return fn()
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001 - every failure reaches the model as Mist's error body
                raise _error(e) from None

        return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)

    # identity

    @server.tool(annotations=READ)
    async def whoami(
        role: Annotated[Optional[str], Field(description="Preview the rights a mint with this role would carry.")] = None,
    ) -> dict:
        """Who you are in Mist: your principal (issuer and id), the buckets you own, your effective rights (from
        ownership, grants and roles) and the roles you may assume. Start here."""
        return await run(lambda: config.client().whoami(role=role))

    @server.tool(annotations=CREATE_IDEMPOTENT)
    async def credentials(
        role: Annotated[Optional[str], Field(description="Mint with this role's rights added (see whoami).")] = None,
    ) -> dict:
        """Temporary S3 credentials (15 minutes) for your identity, with the S3 endpoint. Use them with any S3
        client: path-style addressing, region us-east-1. `env` holds the same values as AWS environment variables
        for the AWS CLI. Call again after they expire or after a new grant. Treat the secret as a secret: do not
        print it or write it to files others can read."""

        def mint():
            m = config.client(role)
            c = m.credentials()
            env = {
                "AWS_ACCESS_KEY_ID": c.access_key_id,
                "AWS_SECRET_ACCESS_KEY": c.secret_access_key,
                "AWS_SESSION_TOKEN": c.session_token,
                "AWS_REGION": REGION,
            }
            if config.s3_endpoint:
                env["AWS_ENDPOINT_URL_S3"] = config.s3_endpoint
            if config.ca_bundle:
                env["AWS_CA_BUNDLE"] = config.ca_bundle
            return {
                "access_key_id": c.access_key_id,
                "secret_access_key": c.secret_access_key,
                "session_token": c.session_token,
                "expiration": c.expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "s3_endpoint": config.s3_endpoint,
                "region": REGION,
                "addressing_style": "path",
                "role": role,
                "env": env,
            }

        return await run(mint)

    # buckets

    @server.tool(annotations=CREATE)
    async def create_bucket(
        name: Annotated[str, Field(description="Bucket name: 3-63 lowercase letters, digits, '-' and '.'.")],
        expiry: Annotated[
            Optional[List[Dict[str, Any]]],
            Field(description='Optional starting expiry rules: [{"prefix": "tmp/", "days": 7}].'),
        ] = None,
    ) -> dict:
        """Create a bucket you own. As owner you may share it (grants, roles), and change its settings."""
        return await run(lambda: config.client().buckets.create(name, expiry=expiry))

    @server.tool(annotations=READ)
    async def get_bucket(name: str) -> dict:
        """A bucket's owner, grants (all of them if you own it, else yours), versioning, expiry rules and quota."""
        return await run(lambda: config.client().buckets.get(name))

    @server.tool(annotations=DELETE)
    async def delete_bucket(name: str) -> dict:
        """Delete an empty bucket you own, and every grant on it. Delete its objects first, over S3."""
        return await run(lambda: config.client().buckets.delete(name))

    # grants

    @server.tool(annotations=CREATE)
    async def create_grant(
        grantee: Grantee,
        bucket: str,
        actions: Actions,
        prefix: Annotated[
            Optional[str], Field(description="The folder, ending in '/'; omit for the whole bucket.")
        ] = None,
        expires: Annotated[Optional[str], Field(description="RFC 3339 time the grant ends; omit for none.")] = None,
        parent: Annotated[
            Optional[str],
            Field(description="To re-share (delegate): the id of a grant to you on this bucket that carries delegate."),
        ] = None,
    ) -> dict:
        """Give another principal (or everyone with a claim value) rights on a bucket folder you own. The grantee
        sees it at their next credentials mint."""
        return await run(
            lambda: config.client().grants.create(
                grantee, bucket, list(actions), prefix=prefix, expires=expires, parent=parent
            )
        )

    @server.tool(annotations=READ)
    async def list_grants(
        bucket: Annotated[
            Optional[str], Field(description="A bucket you own: every grant on it. Omit for the grants made to you.")
        ] = None,
    ) -> dict:
        """The grants made to you, or every grant on a bucket you own."""
        return await run(lambda: config.client().grants.list(bucket=bucket) if bucket else config.client().grants.list())

    @server.tool(annotations=DELETE)
    async def delete_grant(id: Annotated[str, Field(description="The grant's id.")]) -> dict:
        """Revoke a grant you gave, or any grant on a bucket you own."""
        return await run(lambda: config.client().grants.delete(id))

    # roles

    @server.tool(annotations=CREATE)
    async def create_role(
        name: Annotated[str, Field(description="a-z, 0-9 and '-', up to 64; not 'mist'.")],
        rights: Annotated[
            List[Dict[str, Any]],
            Field(
                description='Rights on buckets you own: [{"bucket": "b", "prefix": "reports/", '
                '"actions": ["read"]}]. A role cannot carry delegate.'
            ),
        ],
        trust: Annotated[
            List[Dict[str, Any]],
            Field(description="Who may assume it: a list of grantees, each in the create_grant grantee shape."),
        ],
        description: Optional[str] = None,
    ) -> dict:
        """Create a role: a named template of rights on your buckets that trusted principals add to their
        credentials by minting with the role (credentials tool, `role`)."""
        return await run(lambda: config.client().roles.create(name, rights, trust, description=description))

    @server.tool(annotations=READ)
    async def list_roles() -> dict:
        """The roles you may assume."""
        return await run(lambda: config.client().roles.assumable())

    @server.tool(annotations=DELETE)
    async def delete_role(name: str) -> dict:
        """Delete a role you made."""
        return await run(lambda: config.client().roles.delete(name))

    # change events

    @server.tool(annotations=CREATE_IDEMPOTENT)
    async def create_subscription(
        bucket: str,
        prefix: Annotated[Optional[str], Field(description="The folder; omit for the whole bucket.")] = None,
        name: Annotated[
            Optional[str],
            Field(description="Unique per owner. Creating again with the same name returns the same subscription."),
        ] = None,
        suffix: Annotated[Optional[str], Field(description="Only keys ending with this.")] = None,
        actions: Annotated[
            Optional[List[Literal["put", "delete"]]], Field(description="Which changes; omit for both.")
        ] = None,
        include_self: Annotated[Optional[bool], Field(description="Also your own changes (default false).")] = None,
    ) -> dict:
        """Subscribe to changes (puts and deletes) in a bucket folder you can read, delivered to your feed. Then
        call `feed` with its id. The bucket's owner must have events on. Can take up to about 90 s."""
        return await run(
            lambda: config.client().events.subscribe(
                bucket, prefix, name=name, suffix=suffix, actions=actions, include_self=include_self
            )
        )

    @server.tool(annotations=READ)
    async def list_subscriptions(
        bucket: Annotated[
            Optional[str], Field(description="A bucket you own: every subscription on it. Omit for your own.")
        ] = None,
    ) -> dict:
        """Your subscriptions, or every subscription on a bucket you own."""
        return await run(lambda: config.client().events.list(bucket))

    @server.tool(annotations=DELETE)
    async def delete_subscription(id: str) -> dict:
        """Delete a subscription of yours. Can take up to about 90 s."""
        return await run(lambda: config.client().events.delete(id))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    async def feed(
        subscription_id: str,
        after: Annotated[
            Optional[str],
            Field(
                description="A cursor (the last batch's next_cursor) to read after it, 'now' or 'oldest'. Reading "
                "after a cursor acknowledges everything before it. Omit to resume at the stored position."
            ),
        ] = None,
        wait: Annotated[int, Field(ge=0, le=FEED_MAX_WAIT, description="Seconds to wait for a first event (0-25).")] = 20,
        limit: Annotated[Optional[int], Field(ge=1, le=500, description="Most events in the batch (default 100).")] = None,
    ) -> dict:
        """One bounded long-poll of a subscription: returns as soon as events are ready, or after `wait` seconds
        with none (that is normal). Returns `events` and `next_cursor`; call again with after=next_cursor to go on.
        Delivery is at least once: an event may repeat, so make handling idempotent."""
        w = min(max(wait, 0), FEED_MAX_WAIT)

        def read():
            events, cursor = config.client().events.wait(subscription_id, after=after, wait=w, limit=limit)
            return {"events": events, "next_cursor": cursor}

        return await run(read)

    # workspaces

    @server.tool(annotations=CREATE_IDEMPOTENT)
    async def create_workspace(
        bucket: Annotated[str, Field(description="A bucket you own.")],
        ttl_days: Annotated[int, Field(ge=1, description="Whole days the workspace lives.")],
        run_id: Annotated[
            Optional[str],
            Field(
                description="A-Z a-z 0-9 _ -, up to 64. Repeating the call with the same run_id returns the same "
                "workspace. Omit to generate one."
            ),
        ] = None,
        grantee: Annotated[
            Optional[Dict[str, Any]],
            Field(description="Optional: one principal to share the folder with until it expires (grantee shape)."),
        ] = None,
        actions: Annotated[
            Optional[List[Literal["read", "write", "delete", "delegate"]]],
            Field(description="The grantee's actions; default read, write, delete. Only with a grantee."),
        ] = None,
    ) -> dict:
        """Create a run workspace: the folder `workspaces/<ttl_days>d/<run_id>/` in your bucket, whose objects
        expire after ttl_days, with an optional grant to one other agent that ends with it. Write into `prefix`
        over S3."""
        return await run(
            lambda: config.client().workspaces.create(
                bucket, ttl_days, run_id=run_id, grantee=grantee, actions=actions
            )
        )

    @server.tool(annotations=READ)
    async def list_workspaces() -> dict:
        """The workspaces in the buckets you own."""
        return await run(lambda: config.client().workspaces.list())

    # teaching text

    @server.tool(annotations=READ)
    async def guide(
        topic: Annotated[
            Optional[str], Field(description="A topic name; omit to list the topics.")
        ] = None,
    ) -> str:
        """How to use Mist: the teaching text for one topic (getting started, choosing a surface, workspaces,
        sharing, change events, limits and errors). Omit the topic to list them."""
        topics = skill_topics()
        if not topic:
            return "Topics: " + ", ".join(topics)
        name = topic if topic.startswith("mist-") else "mist-" + topic
        if name not in topics:
            raise ToolError(json.dumps({"error": "NotFound", "message": f"no topic {topic!r}", "topics": topics}))
        return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    for tool in server._tool_manager.list_tools():  # the SDK has no public hook for the generated schema
        tool.parameters = plain_schema(tool.parameters)
    return server


def plain_schema(node: Any) -> Any:
    """The input schema without nulls: an optional parameter is simply not required.

    Pydantic writes `Optional[X] = None` as `{"anyOf": [X, {"type": "null"}], "default": null}`. Some model chat
    templates fail on a null default (LM Studio's Nemotron answers HTTP 400), so both go: the `null` branch and the
    `default: null`. The server still accepts an explicit null.
    """
    if isinstance(node, list):
        return [plain_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: plain_schema(v) for k, v in node.items() if not (k == "default" and v is None)}
    any_of = out.get("anyOf")
    if isinstance(any_of, list):
        rest = [s for s in any_of if s != {"type": "null"}]
        if len(rest) == 1 and len(rest) < len(any_of):
            del out["anyOf"]
            for k, v in rest[0].items():
                out.setdefault(k, v)
    return out


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
