"""`mist-credentials`: the AWS `credential_process` helper (docs/design/sdk.md section 3.4).

It mints once through the same `Provider` the SDK uses, prints the credential_process JSON on stdout and exits 0. On
any failure it prints one line on stderr, nothing on stdout, and exits 1. It writes no file, and no message it prints
holds a token, a key secret or a session token.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from typing import Optional

import httpx

from ._http import origin_of
from .credentials import Provider, Timings, scope_policy
from .errors import IdentityError, MistError
from .identity import AWS_TOKEN_FILE_VAR, MIST_TOKEN_FILE_VAR, PrincipalKey, TokenFile

KEY_ID_VAR = "MIST_PRINCIPAL_KEY_ID"
KEY_SECRET_VAR = "MIST_PRINCIPAL_KEY_SECRET"
ENDPOINT_VAR = "MIST_ENDPOINT"


class _Failure(Exception):
    """A user-facing one-line failure."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # a usage error is a failure like any other: one stderr line, exit 1
        raise _Failure(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="mist-credentials", description="Print AWS credential_process JSON for Mist.", exit_on_error=False)
    p.add_argument("--endpoint", help=f"the Mist origin (default: ${ENDPOINT_VAR})")
    p.add_argument("--role", help="the role to assume (default: no role)")
    p.add_argument("--token-file", help=f"a file holding a JWT (default: ${MIST_TOKEN_FILE_VAR} or ${AWS_TOKEN_FILE_VAR})")
    p.add_argument("--ca-bundle", help="a CA bundle file to trust for the endpoint")
    for flag in ("read", "write", "delete"):
        p.add_argument(f"--{flag}", action="append", default=[], metavar="BUCKET/PREFIX",
                       help=f"narrow the session to {flag} on bucket/prefix (repeatable)")
    return p


def _identity(token_file: Optional[str]):
    if token_file:
        return TokenFile(token_file)
    key_id, secret = os.environ.get(KEY_ID_VAR), os.environ.get(KEY_SECRET_VAR)
    if bool(key_id) != bool(secret):
        raise _Failure(f"set both {KEY_ID_VAR} and {KEY_SECRET_VAR}, or neither")
    if key_id and secret:
        return PrincipalKey(key_id, secret)
    return None  # Provider resolves the token file from the environment, or raises naming both variables


def _run(args, http_client: Optional[httpx.Client]) -> dict:
    endpoint = args.endpoint or os.environ.get(ENDPOINT_VAR)
    if not endpoint:
        raise _Failure(f"no endpoint: pass --endpoint or set {ENDPOINT_VAR}")
    try:
        origin = origin_of(endpoint)
    except ValueError as e:
        raise _Failure(str(e)) from None
    identity = _identity(args.token_file)
    policy = scope_policy(read=args.read, write=args.write, delete=args.delete) if (
        args.read or args.write or args.delete) else None
    own = http_client is None
    if own:
        verify = ssl.create_default_context(cafile=args.ca_bundle) if args.ca_bundle else True
        http_client = httpx.Client(verify=verify)
    try:
        provider = Provider(endpoint=origin, identity=identity, role=args.role, policy=policy,
                            http=http_client, timings=Timings(), session_name="mist-credentials")
        creds = provider.refresh()
    finally:
        if own:
            http_client.close()
    return {
        "Version": 1,
        "AccessKeyId": creds.access_key_id,
        "SecretAccessKey": creds.secret_access_key,
        "SessionToken": creds.session_token,
        "Expiration": creds.expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main(argv: Optional[list] = None, http_client: Optional[httpx.Client] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        doc = _run(args, http_client)
    except _Failure as e:
        print(f"mist-credentials: {e}", file=sys.stderr)
        return 1
    except (MistError, IdentityError, ValueError) as e:
        print(f"mist-credentials: {e}".replace("\n", " "), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"mist-credentials: cannot read the CA bundle: {e.strerror or type(e).__name__}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - never a traceback, never a message that could hold a secret
        print(f"mist-credentials: {type(e).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
