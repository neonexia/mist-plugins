"""The Mist HTTP transport (docs/design/sdk.md sections 3.3, 5.3).

`Api.call` makes one operation of the generated table, with auth, retries (decision P8) and typed errors. `Api.send`
makes one authenticated attempt and hands back the raw reply (the feed loop has its own retry rules, section 6).

Retries: `401` once (no request reached a handler). A network error or a 5xx is retried, 3 attempts in all, for GET,
PUT and DELETE, and for a POST only when the caller says it is repeatable. A `503` whose `Retry-After` is a whole number
of seconds within `unavailable_budget` waits that long instead of backing off; past the budget the `Unavailable` is
raised at once. Any other `Retry-After` is ignored (a plain 5xx).
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials as _BotoCredentials

from ._generated.operations import OPERATIONS, Operation
from .credentials import Timings
from .errors import MistError, from_reply
from .identity import PrincipalKey

REGION = "us-east-1"
SERVICE = "mist"
MAX_ATTEMPTS = 3
_LONG_TIMEOUT_OPS = frozenset({"create_subscription", "delete_subscription", "set_bucket_events"})
_REPEATABLE_METHODS = frozenset({"GET", "PUT", "DELETE"})
_SECONDS = re.compile(r"^\d{1,9}$")


def origin_of(endpoint: str) -> str:
    """Scheme, host and port of `endpoint`: a path, userinfo, query or fragment is dropped."""
    try:
        url = httpx.URL(endpoint)
    except httpx.InvalidURL:
        raise ValueError(f"endpoint {endpoint!r} must look like https://host[:port]") from None
    if not url.scheme or not url.host or "%" in url.host:  # httpx percent-encodes a stray bracket into the host
        raise ValueError(f"endpoint {endpoint!r} must look like https://host[:port]")
    return f"{url.scheme}://{url.netloc.decode('ascii')}"


class Api:
    def __init__(self, endpoint: str, identity, http: httpx.Client, timings: Timings) -> None:
        self._origin = origin_of(endpoint)
        self._identity = identity
        self._http = http
        self._timings = timings

    def call(
        self,
        op: str,
        *,
        path: Optional[dict] = None,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
        repeatable: Optional[bool] = None,
    ) -> object:
        """Run `op` and return the parsed JSON (None for an empty body). Raises `MistError` or a subclass."""
        spec = OPERATIONS[op]
        if repeatable is None:
            repeatable = spec.method in _REPEATABLE_METHODS
        budget = self._timings.unavailable_budget
        attempts = 0
        waited = 0.0
        reauthed = False
        while True:
            attempts += 1
            reply, failure = self._attempt(spec, path, query, body, timeout)
            if reply is None:
                if repeatable and attempts < MAX_ATTEMPTS:
                    self._backoff(attempts)
                    continue
                # Outside the except block of _attempt: the httpx exception holds the request and its credentials.
                raise MistError(0, "NetworkError", f"{failure}: could not reach {self._origin}")
            status = reply.status_code
            if 200 <= status < 300:
                return _parse_ok(reply)
            if status == 401 and not reauthed:
                reauthed = True
                attempts -= 1
                continue
            if status >= 500:
                after = _retry_after(reply) if status == 503 else None
                if after is not None and waited + after > budget:
                    raise _error(reply)
                if repeatable and attempts < MAX_ATTEMPTS:
                    if after is None:
                        self._backoff(attempts)
                    else:
                        waited += after
                        if after > 0:
                            time.sleep(after)
                    continue
            raise _error(reply)

    def send(
        self,
        op: str,
        *,
        path: Optional[dict] = None,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
        repeatable: Optional[bool] = None,
    ) -> httpx.Response:
        """One authenticated attempt, no retry, no status check. A network error is a `MistError`."""
        reply, failure = self._attempt(OPERATIONS[op], path, query, body, timeout)
        if reply is None:
            raise MistError(0, "NetworkError", f"{failure}: could not reach {self._origin}")
        return reply

    @property
    def timings(self) -> Timings:
        return self._timings

    def _backoff(self, attempts: int) -> None:
        t = self._timings
        delay = min(t.backoff_max, t.backoff_min * (2 ** (attempts - 1)))
        if delay > 0:
            time.sleep(delay)

    def _attempt(self, spec: Operation, path, query, body, timeout):
        url = self._origin + spec.path.format(**{k: quote(str(v), safe="") for k, v in (path or {}).items()})
        pairs = {k: v for k, v in (query or {}).items() if v is not None}
        if pairs:
            url += "?" + urlencode(pairs, quote_via=quote)
        data = b""
        headers = {}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        headers.update(self._auth(spec.method, url, data, headers))
        if timeout is None:
            timeout = self._timings.api_timeout
            if spec.id in _LONG_TIMEOUT_OPS:
                timeout = max(timeout, self._timings.events_config_timeout)
        try:
            return self._http.request(spec.method, url, content=data or None, headers=headers, timeout=timeout), None
        except httpx.RequestError as e:
            return None, type(e).__name__

    def _auth(self, method: str, url: str, data: bytes, headers: dict) -> dict:
        identity = self._identity
        if isinstance(identity, PrincipalKey):
            request = AWSRequest(method=method, url=url, data=data, headers=dict(headers))
            SigV4Auth(_BotoCredentials(identity.key_id, identity.secret), SERVICE, REGION).add_auth(request)
            return {k: v for k, v in request.headers.items() if k.lower() in ("authorization", "x-amz-date")}
        return {"Authorization": f"Bearer {identity.token()}"}


def _retry_after(reply: httpx.Response) -> Optional[int]:
    value = reply.headers.get("retry-after", "").strip()
    return int(value) if _SECONDS.match(value) else None


def _parse_ok(reply: httpx.Response) -> object:
    if not reply.content:
        return None
    try:
        return reply.json()
    except ValueError:
        raise MistError(reply.status_code, "InvalidResponse", reply.text[:200]) from None


def _error(reply: httpx.Response) -> MistError:
    request_id = reply.headers.get("x-request-id")
    try:
        body = reply.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str) and isinstance(body.get("message"), str):
        return from_reply(reply.status_code, body, request_id)
    return from_reply(reply.status_code, reply.text, request_id)
