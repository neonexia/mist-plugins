"""Mist for AWS Strands Agents: a session manager on Mist, the Mist skills, and the `mist-mcp` server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.s3_session_manager import S3SessionManager

SKILLS_DIR = Path(__file__).parent / "skills"
"""The Mist skills (Agent Skills folders), shipped with this package."""

MCP_COMMAND = ["uvx", "--from", "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp", "mist-mcp"]
"""How to start the `mist-mcp` server (stdio), as its README documents."""

_MIST_ENV = ("MIST_ENDPOINT", "MIST_S3_ENDPOINT", "MIST_IDENTITY_TOKEN_FILE", "MIST_CA_BUNDLE",
             "MIST_PRINCIPAL_KEY_ID", "MIST_PRINCIPAL_KEY_SECRET")


class MistSessionManager(S3SessionManager):
    """Strands' own `S3SessionManager`, on Mist, with the agent's own Mist credentials.

    Sessions, agents and messages are stored as Strands lays them out (`<prefix>session_<id>/...`) in `bucket`.

    Args:
        session_id: The session to create or resume.
        bucket: The bucket that holds sessions. The agent must own it or hold a grant on the prefix.
        prefix: Key prefix, for example `"strands/"` or a run workspace's prefix.
        client: A `mist.Client`; built from the environment when omitted.
        s3: A boto3 S3 client to use instead of `client.s3()` (tests).
    """

    def __init__(self, session_id: str, bucket: str, prefix: str = "", *, client: Any = None, s3: Any = None, **kwargs: Any):
        if s3 is None:
            if client is None:
                import mist

                client = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))
            s3 = client.s3()
        # S3SessionManager.__init__ builds its own client from a boto3 Session, which cannot carry Mist's CA bundle
        # or its re-mint on 403; set the fields it uses and run the base session manager directly.
        self.bucket = bucket
        self.prefix = prefix
        self.client = s3
        RepositorySessionManager.__init__(self, session_id=session_id, session_repository=self, **kwargs)


def skills_plugin(**kwargs: Any) -> Any:
    """The Strands `AgentSkills` plugin loaded with the Mist skills: `Agent(plugins=[skills_plugin()])`."""
    from strands.vended_plugins.skills import AgentSkills

    return AgentSkills(skills=[str(SKILLS_DIR)], **kwargs)


def mcp_client(command: list[str] | None = None, **kwargs: Any) -> Any:
    """A Strands `MCPClient` for the `mist-mcp` server over stdio, with the Mist settings from this environment.

    Use it as a tool provider: `Agent(tools=[mcp_client()])`.
    """
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    cmd = command or MCP_COMMAND
    env = {k: os.environ[k] for k in ("PATH", "HOME", *_MIST_ENV) if k in os.environ}
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
    return MCPClient(lambda: stdio_client(params), **kwargs)


__all__ = ["MCP_COMMAND", "SKILLS_DIR", "MistSessionManager", "mcp_client", "skills_plugin"]
