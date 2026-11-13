"""Mist for the OpenAI Agents SDK: a session (conversation memory) on Mist, and the `mist-mcp` server."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from agents.memory import SessionABC, SessionSettings
from agents.memory.session_settings import resolve_session_limit
from botocore.exceptions import ClientError

MCP_COMMAND = ["uvx", "--from", "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp", "mist-mcp"]
"""How to start the `mist-mcp` server (stdio), as its README documents."""

_MIST_ENV = ("MIST_ENDPOINT", "MIST_S3_ENDPOINT", "MIST_IDENTITY_TOKEN_FILE", "MIST_CA_BUNDLE",
             "MIST_PRINCIPAL_KEY_ID", "MIST_PRINCIPAL_KEY_SECRET")


class MistSession(SessionABC):
    """An Agents SDK session whose history is one JSON object in Mist: `<prefix><session_id>.json`.

    Pass it to `Runner.run(agent, input, session=MistSession(...))`. Writes replace the whole object, so use one
    session object per conversation at a time.

    Args:
        session_id: The conversation to create or resume.
        bucket: The bucket that holds sessions. The agent must own it or hold a grant on the prefix.
        prefix: Key prefix, for example `"sessions/"` or a run workspace's prefix.
        client: A `mist.Client`; built from the environment when omitted.
        s3: A boto3 S3 client to use instead of `client.s3()` (tests).
        session_settings: The SDK's `SessionSettings` (for example a default `limit`).
    """

    def __init__(
        self,
        session_id: str,
        bucket: str,
        prefix: str = "sessions/",
        *,
        client: Any = None,
        s3: Any = None,
        session_settings: SessionSettings | None = None,
    ) -> None:
        if s3 is None:
            if client is None:
                import mist

                client = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))
            s3 = client.s3()
        self.session_id = session_id
        self.session_settings = session_settings
        self.bucket = bucket
        self.key = (prefix.strip("/") + "/" if prefix.strip("/") else "") + f"{session_id}.json"
        self.s3 = s3
        self._lock = asyncio.Lock()

    def _load(self) -> list:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return []
            raise

    def _save(self, items: list) -> None:
        body = json.dumps(items).encode()
        self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=body, ContentType="application/json")

    async def get_items(self, limit: int | None = None) -> list:
        items = await asyncio.to_thread(self._load)
        limit = resolve_session_limit(limit, self.session_settings)
        return items if limit is None else items[-limit:] if limit > 0 else []

    async def add_items(self, items: list) -> None:
        if not items:
            return
        async with self._lock:
            stored = await asyncio.to_thread(self._load)
            await asyncio.to_thread(self._save, stored + list(items))

    async def pop_item(self) -> Any:
        async with self._lock:
            stored = await asyncio.to_thread(self._load)
            if not stored:
                return None
            item = stored.pop()
            await asyncio.to_thread(self._save, stored)
            return item

    async def clear_session(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.s3.delete_object, Bucket=self.bucket, Key=self.key)


def mcp_server(command: list[str] | None = None, **kwargs: Any) -> Any:
    """An Agents SDK `MCPServerStdio` running `mist-mcp` with the Mist settings from this environment.

    `async with mcp_server() as mist: Agent(..., mcp_servers=[mist])`. The timeout allows the `feed` tool's 25 s
    long-poll.
    """
    from agents.mcp import MCPServerStdio

    cmd = command or MCP_COMMAND
    env = {k: os.environ[k] for k in ("PATH", "HOME", *_MIST_ENV) if k in os.environ}
    kwargs.setdefault("name", "mist")
    kwargs.setdefault("client_session_timeout_seconds", 60)
    return MCPServerStdio(params={"command": cmd[0], "args": cmd[1:], "env": env}, **kwargs)


__all__ = ["MCP_COMMAND", "MistSession", "mcp_server"]
