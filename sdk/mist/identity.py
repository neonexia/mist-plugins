"""Identity sources (docs/design/sdk.md section 3.1).

An identity source returns a JWT (`token()`), or is a principal key the provider signs STS `AssumeRole` with. The SDK
looks in exactly one place on its own: the token file named by `MIST_IDENTITY_TOKEN_FILE`, else
`AWS_WEB_IDENTITY_TOKEN_FILE`. The SDK never writes a token to disk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Union

from .errors import IdentityError

MIST_TOKEN_FILE_VAR = "MIST_IDENTITY_TOKEN_FILE"
AWS_TOKEN_FILE_VAR = "AWS_WEB_IDENTITY_TOKEN_FILE"


class TokenFile:
    """A file holding a JWT, read again on every call so a token the platform rotates refreshes for free."""

    def __init__(self, path: Union[str, "os.PathLike[str]", None] = None) -> None:
        if path is None:
            path = os.environ.get(MIST_TOKEN_FILE_VAR) or os.environ.get(AWS_TOKEN_FILE_VAR)
            if not path:
                raise IdentityError(
                    f"no identity: pass identity=, or set {MIST_TOKEN_FILE_VAR} or {AWS_TOKEN_FILE_VAR} "
                    "to a file holding a JWT"
                )
        self.path = Path(path)

    def token(self) -> str:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise IdentityError(f"cannot read identity token file {self.path}: {e.strerror or type(e).__name__}") from None
        token = text.strip()
        if not token:
            raise IdentityError(f"identity token file {self.path} is empty")
        return token

    def __repr__(self) -> str:
        return f"TokenFile({str(self.path)!r})"


class Token:
    """A JWT the caller passes. It cannot refresh past its own expiry."""

    def __init__(self, jwt: str) -> None:
        self._jwt = jwt

    def token(self) -> str:
        if not self._jwt:
            raise IdentityError("the identity token is empty")
        return self._jwt

    def __repr__(self) -> str:
        return "Token(<hidden>)"


class Callback:
    """The caller's function returns a fresh JWT each time the SDK asks."""

    def __init__(self, fn: Callable[[], str]) -> None:
        self.fn = fn

    def token(self) -> str:
        token = self.fn()
        if not isinstance(token, str) or not token.strip():
            raise IdentityError("the identity callback returned no token")
        return token.strip()

    def __repr__(self) -> str:
        return f"Callback({self.fn!r})"


class PrincipalKey:
    """A principal key: STS `AssumeRole` signed with SigV4. Has no token."""

    def __init__(self, key_id: str, secret: str) -> None:
        self.key_id = key_id
        self.secret = secret

    def __repr__(self) -> str:
        return f"PrincipalKey({self.key_id!r}, <hidden>)"


Identity = Union[TokenFile, Token, Callback, PrincipalKey]


def resolve(identity: Optional[Identity]) -> Identity:
    """An explicit identity wins; with none, the token file from the environment. No further chain."""
    if identity is None:
        return TokenFile()
    if isinstance(identity, (TokenFile, Token, Callback, PrincipalKey)):
        return identity
    raise TypeError(
        f"identity must be TokenFile, Token, Callback or PrincipalKey, not {type(identity).__name__}"
    )
