"""Errors the SDK raises (docs/design/sdk.md section 5.3).

Every Mist HTTP error is `{"error", "message"}` plus `rule` on 403 and 409. STS errors (XML) become the same type with
the STS `<Code>` in `error`. No message ever carries a token, secret or session token.
"""
from __future__ import annotations

from typing import Optional


class MistError(Exception):
    """A refused or failed Mist call. Branch on `error` and `rule`."""

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        rule: Optional[str] = None,
        request_id: Optional[str] = None,
        body: object = None,
    ) -> None:
        self.status = status
        self.error = error
        self.message = message
        self.rule = rule
        self.request_id = request_id
        self.body = body
        text = f"{status} {error}: {message}"
        if rule:
            text += f" (rule: {rule})"
        if request_id:
            text += f" [request {request_id}]"
        super().__init__(text)


class AccessDenied(MistError):
    """`error == "AccessDenied"`; `rule` says why."""


class NotFound(MistError):
    """404."""


class Conflict(MistError):
    """409; `error` names the conflict."""


class CursorExpired(MistError):
    """410: restart from `oldest_cursor` or `now_cursor` (either may be None)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        body = self.body if isinstance(self.body, dict) else {}
        self.oldest_cursor: Optional[str] = body.get("oldest_cursor")
        self.now_cursor: Optional[str] = body.get("now_cursor")


class Unavailable(MistError):
    """503: retryable."""


class IdentityError(Exception):
    """No identity source, or a source that cannot produce a token."""


_BY_STATUS = {404: NotFound, 409: Conflict, 410: CursorExpired, 503: Unavailable}

_MAX_TEXT = 200


def from_reply(status: int, body: object, request_id: Optional[str]) -> MistError:
    """Build the error for a reply: the subclass is picked by `error` (AccessDenied) or by status."""
    if isinstance(body, dict):
        error = str(body.get("error") or "")
        message = str(body.get("message") or "")
        rule = body.get("rule")
        request_id = body.get("request_id") or request_id
    else:
        # A body that is not the error shape (413 from the HTTP framework, a proxy's HTML page): keep a short excerpt.
        error = "InvalidResponse"
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
        message = text[:_MAX_TEXT]
        rule = None
    cls = AccessDenied if error == "AccessDenied" else _BY_STATUS.get(status, MistError)
    return cls(status, error, message, rule=rule, request_id=request_id, body=body)
