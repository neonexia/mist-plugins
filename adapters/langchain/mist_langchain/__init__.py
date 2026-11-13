"""Mist for LangChain Deep Agents and LangGraph: a file backend, a store and a checkpointer, all on S3 in Mist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backend import MistBackend
from .checkpoint import MistSaver
from .store import MistStore

SKILLS_DIR = Path(__file__).parent / "skills"
"""The Mist skills (Agent Skills folders), shipped with this package."""

SKILLS_ROUTE = "/skills/mist/"
"""Where `deep_agent_backend` mounts the Mist skills: pass `skills=[SKILLS_ROUTE]` to `create_deep_agent`."""


def deep_agent_backend(bucket: str, prefix: str = "", *, client: Any = None, s3: Any = None) -> Any:
    """A Deep Agents backend: files in Mist, plus the Mist skills (read-only) at `SKILLS_ROUTE`.

    Use it with `create_deep_agent(backend=deep_agent_backend(...), skills=[SKILLS_ROUTE])`.
    """
    from deepagents.backends import CompositeBackend, FilesystemBackend

    return CompositeBackend(
        default=MistBackend(bucket, prefix, client=client, s3=s3),
        routes={SKILLS_ROUTE: FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)},
    )


__all__ = ["MistBackend", "MistSaver", "MistStore", "SKILLS_DIR", "SKILLS_ROUTE", "deep_agent_backend"]
