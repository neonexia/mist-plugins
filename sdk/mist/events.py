"""Event subscriptions: the control calls and the feed (docs/design/sdk.md sections 5.1 and 6)."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterator, List, Optional, Set, Tuple, Union

import httpx

from ._generated import models
from ._http import Api, _error, _parse_ok, _retry_after
from .errors import CursorExpired, MistError

SEEN_IDS = 1000


@dataclass(frozen=True)
class Gap:
    """`stream()` restarted at `cursor` after a 410; `reason` is "oldest" or "now". Re-list the prefix to resync."""

    cursor: str
    reason: str


class Events:
    def __init__(self, api: Api) -> None:
        self._api = api

    def subscribe(
        self,
        bucket: str,
        prefix: Optional[str] = None,
        *,
        name: Optional[str] = None,
        delivery: Optional[dict] = None,
        suffix: Optional[str] = None,
        actions: Optional[list] = None,
        include_self: Optional[bool] = None,
        exclude_actors: Optional[list] = None,
    ) -> models.Subscription:
        """Create a subscription. A named one is idempotent (the same name returns the same subscription), so it is
        the only POST the transport retries."""
        body = _body(
            bucket=bucket, prefix=prefix, name=name, delivery=delivery, suffix=suffix, actions=actions,
            include_self=include_self, exclude_actors=exclude_actors,
        )
        return self._api.call("create_subscription", body=body, repeatable=name is not None)  # type: ignore[return-value]

    def list(self, bucket: Optional[str] = None) -> models.SubscriptionList:
        return self._api.call("list_subscriptions", query={"bucket": bucket})  # type: ignore[return-value]

    def get(self, id: str) -> models.Subscription:
        return self._api.call("get_subscription", path={"id": id})  # type: ignore[return-value]

    def delete(self, id: str) -> models.Deleted:
        return self._api.call("delete_subscription", path={"id": id})  # type: ignore[return-value]

    def rotate_secret(self, id: str) -> models.RotatedSecret:
        return self._api.call("rotate_secret", path={"id": id})  # type: ignore[return-value]

    def resume(self, id: str) -> models.ResumedWebhook:
        return self._api.call("resume_webhook", path={"id": id})  # type: ignore[return-value]

    def deliveries(self, id: str) -> models.DeliveryList:
        return self._api.call("list_deliveries", path={"id": id})  # type: ignore[return-value]

    def redeliver(self, id: str, delivery_id: str) -> models.Redelivery:
        return self._api.call("redeliver", path={"id": id, "delivery": delivery_id})  # type: ignore[return-value]

    def wait(
        self, id: str, *, after: Optional[str] = None, wait: int = 25, limit: Optional[int] = None
    ) -> Tuple[List[models.Event], str]:
        """One long-poll: `(events, next_cursor)`. An empty list after `wait` seconds is normal. Reading with `after=X`
        acknowledges everything up to X, so pass the cursor of the last batch you have finished with."""
        reply = self._read(id, after, wait, limit)
        if not 200 <= reply.status_code < 300:
            raise _error(reply)
        return _batch(reply)  # type: ignore[return-value]

    def stream(
        self, id: str, *, after: Optional[str] = None, wait: int = 25, on_expired: Optional[str] = None
    ) -> Iterator[Union[models.Event, Gap]]:
        """Events forever, from `wait()` in a loop. The next read (which acknowledges this batch) is sent only when the
        consumer asks for the next event after the batch. No cursor is kept on disk."""
        if on_expired not in (None, "oldest", "now"):
            raise ValueError('on_expired must be None, "oldest" or "now"')
        return self._stream(id, after, wait, on_expired)

    def _stream(self, id, after, wait, on_expired):
        t = self._api.timings
        seen: Deque[str] = deque()
        seen_set: Set[str] = set()
        failures = 0
        while True:
            try:
                reply = self._read(id, after, wait, None)
            except MistError as e:
                if e.status != 0:
                    raise
                delay = min(t.backoff_max, t.backoff_min * (2 ** min(failures, 30)))
                failures += 1
                if delay > 0:
                    time.sleep(delay)
                continue
            status = reply.status_code
            if status in (500, 502, 504):
                # A gateway fault is transient on a long-lived feed: back off like a network error and keep going.
                delay = min(t.backoff_max, t.backoff_min * (2 ** min(failures, 30)))
                failures += 1
                if delay > 0:
                    time.sleep(delay)
                continue
            if status == 503:
                retry = _retry_after(reply)
                delay = min(max(retry, t.backoff_min), t.backoff_max) if retry is not None else min(t.backoff_max, t.backoff_min * (2 ** min(failures, 30)))
                failures += 1
                if delay > 0:
                    time.sleep(delay)
                continue
            failures = 0
            if status == 410 and on_expired is not None:
                err = _error(reply)
                cursor = err.oldest_cursor if on_expired == "oldest" else err.now_cursor  # type: ignore[attr-defined]
                if not cursor:
                    raise err
                after = cursor
                yield Gap(cursor, on_expired)
                continue
            if not 200 <= status < 300:
                raise _error(reply)
            events, next_cursor = _batch(reply)
            for event in events:
                if event["id"] in seen_set:
                    continue
                seen.append(event["id"])
                seen_set.add(event["id"])
                if len(seen) > SEEN_IDS:
                    seen_set.discard(seen.popleft())
                yield event
            after = next_cursor

    def _read(self, id: str, after: Optional[str], wait: int, limit: Optional[int]) -> httpx.Response:
        """One feed request (a `401` is sent once more with a fresh token); no other retry."""
        args = dict(
            path={"id": id}, query={"after": after, "wait": wait, "limit": limit}, timeout=wait + 10
        )
        reply = self._api.send("feed", **args)
        if reply.status_code == 401:
            reply = self._api.send("feed", **args)
        return reply


def _batch(reply: httpx.Response) -> Tuple[list, str]:
    data = _parse_ok(reply)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or not isinstance(
        data.get("next_cursor"), str
    ):
        raise MistError(reply.status_code, "InvalidResponse", "the feed reply is not an event batch")
    return data["events"], data["next_cursor"]


def _body(**fields) -> dict:
    """The request body: a None argument is left out."""
    return {k: v for k, v in fields.items() if v is not None}
