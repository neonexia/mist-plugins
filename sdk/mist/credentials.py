"""The refreshing credential provider (docs/design/sdk.md sections 3.2 and 3.3).

STS is called over httpx (a form POST; the XML reply is parsed with `xml.etree`). A principal-key mint is signed
with botocore's SigV4 (service `sts`, region `us-east-1`). Refresh follows botocore's shape (decision P4):

- more than `refresh_before` (300 s) left: the cached credentials are returned;
- at most `refresh_before` but more than `block_before` (120 s) left: the first caller that gets the lock re-mints,
  callers that do not get it keep the current credentials, and a failed mint is ignored;
- at most `block_before` left, or nothing cached: callers block on the lock and a failed mint raises.

There is no background thread. Every mint sends the same RoleArn and Policy, so a re-mint never widens a narrowed
session. Nothing is written to disk.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Union
from urllib.parse import urlencode

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials as _BotoCredentials

from .errors import MistError, from_reply
from .identity import Identity, PrincipalKey, resolve

ROLE_ARN_PREFIX = "arn:aws:iam::000000000000:role/"
NO_ROLE = "mist"
STS_VERSION = "2011-06-15"
STS_REGION = "us-east-1"
_FORM = "application/x-www-form-urlencoded; charset=utf-8"


# The one address serves STS here (the gateway route); S3 and the Mist API share the same host.
STS_PATH = "/mist/v1/sts"


@dataclass(frozen=True)
class Credentials:
    """Temporary S3 credentials. `expiration` is timezone-aware UTC."""

    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expiration: datetime


@dataclass(frozen=True)
class Timings:
    """Every timed behaviour of the SDK, in seconds."""

    refresh_before: float = 300
    block_before: float = 120
    api_timeout: float = 30
    events_config_timeout: float = 120
    unavailable_budget: float = 30
    backoff_min: float = 0.5
    backoff_max: float = 15


class Provider:
    """Mints credentials through the gateway's STS and refreshes them before they expire. Thread-safe."""

    def __init__(
        self,
        *,
        endpoint: str,
        identity: Optional[Identity],
        role: Optional[str],
        policy: Optional[dict],
        http: httpx.Client,
        timings: Timings,
        session_name: str = "mist-sdk",
    ) -> None:
        self._url = endpoint.rstrip("/") + STS_PATH
        url = httpx.URL(self._url)
        # httpx's netloc is host and port only (IPv6 kept in brackets): userinfo never reaches an error message.
        self._origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
        self._identity = resolve(identity)
        self._role_arn = ROLE_ARN_PREFIX + (role or NO_ROLE)
        # Serialized now: a caller changing its dict later cannot change (or widen) a later mint.
        self._policy = json.dumps(copy.deepcopy(policy), separators=(",", ":")) if policy is not None else None
        self._http = http
        self._timings = timings
        self._session_name = session_name
        self._lock = threading.Lock()
        self._creds: Optional[Credentials] = None

    @property
    def identity(self) -> Identity:
        return self._identity

    def get(self) -> Credentials:
        """Cached credentials, refreshed as the module docstring describes."""
        seen = self._creds
        if seen is not None:
            left = _seconds_left(seen)
            if left > self._timings.refresh_before:
                return seen
            if left > self._timings.block_before:
                # Advisory refresh: one caller re-mints, the others keep what they have; a failure is ignored.
                if self._lock.acquire(blocking=False):
                    try:
                        if self._creds is seen:
                            try:
                                self._creds = self._mint()
                            except Exception:  # noqa: BLE001 - the current credentials are still good
                                pass
                    finally:
                        self._lock.release()
                return self._creds or seen
        # Mandatory refresh: block; a failure raises.
        with self._lock:
            if self._creds is not seen:
                # Another caller minted while this one waited. Use its credentials even if they look expired
                # (a skewed clock), rather than minting once per waiting caller.
                return self._creds  # type: ignore[return-value]
            self._creds = self._mint()
            return self._creds

    def refresh(self) -> Credentials:
        """Mint now, whatever the cache holds (for example after a new grant). A failure raises."""
        with self._lock:
            self._creds = self._mint()
            return self._creds

    def _mint(self) -> Credentials:
        form = {"Version": STS_VERSION, "RoleSessionName": self._session_name, "RoleArn": self._role_arn}
        if self._policy is not None:
            form["Policy"] = self._policy
        identity = self._identity
        if isinstance(identity, PrincipalKey):
            form["Action"] = "AssumeRole"
            body = urlencode(form).encode()
            request = AWSRequest(method="POST", url=self._url, data=body, headers={"Content-Type": _FORM})
            SigV4Auth(_BotoCredentials(identity.key_id, identity.secret), "sts", STS_REGION).add_auth(request)
            headers = dict(request.headers.items())
        else:
            form["Action"] = "AssumeRoleWithWebIdentity"
            form["WebIdentityToken"] = identity.token()
            body = urlencode(form).encode()
            headers = {"Content-Type": _FORM}
        failure: Optional[str] = None
        try:
            reply = self._http.post(self._url, content=body, headers=headers, timeout=self._timings.api_timeout)
        except httpx.RequestError as e:
            failure = type(e).__name__
        if failure is not None:
            # Raised outside the except block so neither __cause__ nor __context__ links the httpx exception: it
            # holds the request, whose body carries the WebIdentityToken.
            raise MistError(0, "NetworkError", f"{failure}: could not reach STS at {self._origin}")
        return _parse_reply(reply)


def _seconds_left(creds: Credentials) -> float:
    return (creds.expiration - datetime.now(timezone.utc)).total_seconds()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ET.Element, name: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == name:
            return el.text or ""
    return None


def _parse_reply(reply: httpx.Response) -> Credentials:
    try:
        root: Optional[ET.Element] = ET.fromstring(reply.content)
    except ET.ParseError:
        root = None
    if reply.status_code != 200:
        code = _find(root, "Code") if root is not None else None
        message = _find(root, "Message") if root is not None else None
        request_id = _find(root, "RequestId") if root is not None else None
        # A reply that is not STS XML: from_reply makes it InvalidResponse with a short excerpt.
        body: object = reply.text if code is None else {"error": code, "message": message or ""}
        raise from_reply(reply.status_code, body, request_id or reply.headers.get("x-amzn-requestid"))
    fields = {}
    if root is not None:
        for name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"):
            fields[name] = _find(root, name)
    if root is None or not all(fields.values()):
        raise MistError(reply.status_code, "InvalidResponse", "the STS reply holds no credentials")
    return Credentials(
        access_key_id=fields["AccessKeyId"],
        secret_access_key=fields["SecretAccessKey"],
        session_token=fields["SessionToken"],
        expiration=_parse_time(fields["Expiration"]),
    )


_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$")


def _parse_time(text: str) -> datetime:
    """An ISO 8601 timestamp with any number of fraction digits; no zone means UTC (Python 3.10 safe)."""
    m = _TIME.match(text.strip())
    if not m:
        raise MistError(200, "InvalidResponse", f"the STS reply has an unreadable Expiration {text[:40]!r}")
    t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    if m.group(2):
        t += timedelta(microseconds=int(m.group(2)[:6].ljust(6, "0")))
    zone = m.group(3)
    if zone in (None, "Z"):
        return t.replace(tzinfo=timezone.utc)
    sign = 1 if zone[0] == "+" else -1
    digits = zone[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return t.replace(tzinfo=timezone(sign * offset)).astimezone(timezone.utc)


def _entries(value: Union[str, Iterable[str]]) -> list:
    return [value] if isinstance(value, str) else list(value)


def _split(entry: str) -> tuple:
    bucket, _, prefix = entry.partition("/")
    if not bucket:
        raise ValueError(f"scope entry {entry!r} must be bucket or bucket/prefix")
    return bucket, prefix


def scope_policy(
    read: Union[str, Iterable[str]] = (),
    write: Union[str, Iterable[str]] = (),
    delete: Union[str, Iterable[str]] = (),
) -> dict:
    """The self-narrowing `Policy` for `bucket/prefix` entries (`"datasets"` or `"datasets/"` is the whole bucket).

    The gateway enforces the grammar and the 2048-byte cap; this helper checks neither (no client-side authorization).
    """
    statements = []
    for entry in _entries(read):
        bucket, prefix = _split(entry)
        statements.append(
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/{prefix}*"}
        )
        listing = {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"}
        if prefix:
            listing["Condition"] = {"StringLike": {"s3:prefix": f"{prefix}*"}}
        statements.append(listing)
    for action, entries in (("s3:PutObject", write), ("s3:DeleteObject", delete)):
        for entry in _entries(entries):
            bucket, prefix = _split(entry)
            statements.append({"Effect": "Allow", "Action": [action], "Resource": f"arn:aws:s3:::{bucket}/{prefix}*"})
    return {"Version": "2012-10-17", "Statement": statements}
