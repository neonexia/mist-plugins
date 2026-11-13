"""A LangGraph checkpointer on Mist. Same data model as LangGraph's `InMemorySaver`, kept in S3 objects."""

from __future__ import annotations

import asyncio
import base64
import json
import random
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)

from ._s3 import dec, delete, enc, get, keys, norm_prefix, s3_client

# Layout under <prefix><thread>/<ns>/:
#   checkpoints/<checkpoint_id>          {"checkpoint", "metadata", "parent"}   (serde-typed, base64)
#   blobs/<channel>/<version>            one channel value                       (serde-typed, base64)
#   writes/<checkpoint_id>/<task>/<idx>  {"channel", "value", "task_path"}


def _pack(typed: tuple[str, bytes]) -> list[str]:
    return [typed[0], base64.b64encode(typed[1]).decode("ascii")]


def _unpack(packed: list[str]) -> tuple[str, bytes]:
    return packed[0], base64.b64decode(packed[1])


class MistSaver(BaseCheckpointSaver[str]):
    """LangGraph checkpointer on Mist.

    Args:
        bucket: The bucket for checkpoints. The agent must own it or hold a grant on the prefix.
        prefix: Key prefix, for example `"langgraph/checkpoints/"`.
        client: A `mist.Client`; built from the environment when omitted.
        s3: A boto3 S3 client to use instead of `client.s3()` (tests).
        serde: A LangGraph serializer; the default is LangGraph's own.
    """

    def __init__(self, bucket: str, prefix: str = "checkpoints/", *, client: Any = None, s3: Any = None, serde: Any = None) -> None:
        super().__init__(serde=serde)
        self.bucket = bucket
        self.prefix = norm_prefix(prefix)
        self.s3 = s3 or s3_client(client)

    # -- object helpers -----------------------------------------------------------------------------------------

    def _base(self, thread_id: str, ns: str) -> str:
        return f"{self.prefix}{enc(thread_id)}/{enc(ns)}/"

    def _json(self, key: str) -> Any:
        data = get(self.s3, self.bucket, key)
        return None if data is None else json.loads(data)

    def _put_json(self, key: str, doc: Any) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(doc).encode(), ContentType="application/json")

    def _tuple(self, thread_id: str, ns: str, checkpoint_id: str, config: RunnableConfig | None = None) -> CheckpointTuple | None:
        base = self._base(thread_id, ns)
        doc = self._json(f"{base}checkpoints/{enc(checkpoint_id)}")
        if doc is None:
            return None
        checkpoint: Checkpoint = self.serde.loads_typed(_unpack(doc["checkpoint"]))
        values = {}
        for channel, version in checkpoint["channel_versions"].items():
            blob = self._json(f"{base}blobs/{enc(channel)}/{enc(str(version))}")
            if blob is not None and blob[0] != "empty":
                values[channel] = self.serde.loads_typed(_unpack(blob))
        writes = []
        for key in keys(self.s3, self.bucket, f"{base}writes/{enc(checkpoint_id)}/"):
            w = self._json(key)
            if w is not None:
                writes.append((w["task_path"], dec(key.split("/")[-2]), int(dec(key.split("/")[-1])), w))
        writes.sort(key=lambda t: writes_sort_key(t[0], t[1], t[2]))
        here = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}
        return CheckpointTuple(
            config=config if config and get_checkpoint_id(config) else here,
            checkpoint={**checkpoint, "channel_values": values},
            metadata=self.serde.loads_typed(_unpack(doc["metadata"])),
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": doc["parent"]}}
                if doc["parent"]
                else None
            ),
            pending_writes=[(task, w["channel"], self.serde.loads_typed(_unpack(w["value"]))) for _, task, _, w in writes],
        )

    def _checkpoint_ids(self, thread_id: str, ns: str) -> list[str]:
        prefix = self._base(thread_id, ns) + "checkpoints/"
        return sorted((dec(k[len(prefix) :]) for k in keys(self.s3, self.bucket, prefix)), reverse=True)

    # -- BaseCheckpointSaver ------------------------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            ids = self._checkpoint_ids(thread_id, ns)
            if not ids:
                return None
            checkpoint_id = ids[0]
        return self._tuple(thread_id, ns, checkpoint_id, config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config:
            threads = [(config["configurable"]["thread_id"], config["configurable"].get("checkpoint_ns"))]
        else:
            threads = sorted({(dec(k[len(self.prefix) :].split("/")[0]), None) for k in keys(self.s3, self.bucket, self.prefix)})
        wanted = get_checkpoint_id(config) if config else None
        before_id = get_checkpoint_id(before) if before else None
        for thread_id, ns_filter in threads:
            thread_prefix = f"{self.prefix}{enc(thread_id)}/"
            namespaces = sorted({dec(k[len(thread_prefix) :].split("/")[0]) for k in keys(self.s3, self.bucket, thread_prefix)})
            for ns in namespaces:
                if ns_filter is not None and ns != ns_filter:
                    continue
                for checkpoint_id in self._checkpoint_ids(thread_id, ns):
                    if wanted and checkpoint_id != wanted:
                        continue
                    if before_id and checkpoint_id >= before_id:
                        continue
                    tup = self._tuple(thread_id, ns, checkpoint_id)
                    if tup is None or (filter and not all(tup.metadata.get(k) == v for k, v in filter.items())):
                        continue
                    if limit is not None:
                        if limit <= 0:
                            return
                        limit -= 1
                    yield tup

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        base = self._base(thread_id, ns)
        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        for channel, version in new_versions.items():
            typed = self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")
            self._put_json(f"{base}blobs/{enc(channel)}/{enc(str(version))}", _pack(typed))
        self._put_json(
            f"{base}checkpoints/{enc(checkpoint['id'])}",
            {
                "checkpoint": _pack(self.serde.dumps_typed(c)),
                "metadata": _pack(self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))),
                "parent": config["configurable"].get("checkpoint_id"),
            },
        )
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        prefix = f"{self._base(thread_id, ns)}writes/{enc(checkpoint_id)}/{enc(task_id)}/"
        existing = set(keys(self.s3, self.bucket, prefix)) if writes else set()
        for idx, (channel, value) in enumerate(writes):
            i = WRITES_IDX_MAP.get(channel, idx)
            key = prefix + enc(str(i))
            if i >= 0 and key in existing:
                continue  # regular writes are stored once; special writes (errors, interrupts) overwrite
            self._put_json(key, {"channel": channel, "value": _pack(self.serde.dumps_typed(value)), "task_path": task_path})

    def delete_thread(self, thread_id: str) -> None:
        delete(self.s3, self.bucket, keys(self.s3, self.bucket, f"{self.prefix}{enc(thread_id)}/"))

    def get_next_version(self, current: str | None, channel: None = None) -> str:
        current_v = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    # -- async: the sync calls in a thread ----------------------------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
