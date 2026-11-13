"""A LangGraph store on Mist: one JSON object per item, under one bucket and prefix."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from langgraph.store.memory import _compare_values, _does_match

from ._s3 import dec, delete, enc, get, keys, norm_prefix, s3_client


class MistStore(BaseStore):
    """LangGraph `BaseStore` on Mist.

    The item `(namespace, key)` is the object `<prefix><ns...>/<key>` holding
    `{"value": ..., "created_at": ..., "updated_at": ...}`. Search filters on values; there is no vector index, so a
    `query` is ignored and every `score` is `None`.

    Args:
        bucket: The bucket for the store. The agent must own it or hold a grant on the prefix.
        prefix: Key prefix, for example `"langgraph/store/"`.
        client: A `mist.Client`; built from the environment when omitted.
        s3: A boto3 S3 client to use instead of `client.s3()` (tests).
    """

    def __init__(self, bucket: str, prefix: str = "store/", *, client: Any = None, s3: Any = None) -> None:
        self.bucket = bucket
        self.prefix = norm_prefix(prefix)
        self.s3 = s3 or s3_client(client)

    def _ns_prefix(self, namespace: tuple[str, ...]) -> str:
        return self.prefix + "".join(enc(p) + "/" for p in namespace)

    def _key(self, namespace: tuple[str, ...], key: str) -> str:
        return self._ns_prefix(namespace) + enc(key)

    def _split(self, object_key: str) -> tuple[tuple[str, ...], str]:
        segs = object_key[len(self.prefix) :].split("/")
        return tuple(dec(s) for s in segs[:-1]), dec(segs[-1])

    def _load(self, object_key: str) -> dict | None:
        data = get(self.s3, self.bucket, object_key)
        return None if data is None else json.loads(data)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        return [self._run(op) for op in ops]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, list(ops))

    def _run(self, op: Op) -> Result:
        if isinstance(op, GetOp):
            doc = self._load(self._key(op.namespace, op.key))
            return None if doc is None else Item(namespace=op.namespace, key=op.key, **doc)
        if isinstance(op, PutOp):
            return self._put(op)
        if isinstance(op, SearchOp):
            return self._search(op)
        if isinstance(op, ListNamespacesOp):
            return self._list_namespaces(op)
        raise ValueError(f"unknown operation {op!r}")

    def _put(self, op: PutOp) -> None:
        object_key = self._key(op.namespace, op.key)
        if op.value is None:
            self.s3.delete_object(Bucket=self.bucket, Key=object_key)
            return None
        now = datetime.now(timezone.utc).isoformat()
        old = self._load(object_key)
        doc = {"value": op.value, "created_at": old["created_at"] if old else now, "updated_at": now}
        self.s3.put_object(Bucket=self.bucket, Key=object_key, Body=json.dumps(doc).encode(), ContentType="application/json")
        return None

    def _search(self, op: SearchOp) -> list[SearchItem]:
        items = []
        for object_key in keys(self.s3, self.bucket, self._ns_prefix(op.namespace_prefix)):
            doc = self._load(object_key)
            if doc is None:
                continue
            if op.filter and not all(_compare_values(doc["value"].get(k), v) for k, v in op.filter.items()):
                continue
            namespace, key = self._split(object_key)
            items.append(SearchItem(namespace=namespace, key=key, **doc))
        items.sort(key=lambda i: i.updated_at, reverse=True)
        return items[op.offset : op.offset + op.limit]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        namespaces = {self._split(k)[0] for k in keys(self.s3, self.bucket, self.prefix)}
        if op.match_conditions:
            namespaces = {ns for ns in namespaces if all(_does_match(c, ns) for c in op.match_conditions)}
        if op.max_depth is not None:
            namespaces = {ns[: op.max_depth] for ns in namespaces}
        return sorted(namespaces)[op.offset : op.offset + op.limit]

    def clear(self) -> None:
        """Delete every item under the prefix."""
        delete(self.s3, self.bucket, keys(self.s3, self.bucket, self.prefix))
