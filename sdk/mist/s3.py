"""`mist.s3()`: botocore hooks over the refreshing provider (docs/design/sdk.md section 4, decision P5).

Two public botocore hooks, both on the botocore session so every S3 client built from `boto3_session()` has them:

- a `credential_provider` component whose credentials read `Provider.get()` on every access, so botocore never holds
  its own copy and never applies its 15-minute `RefreshableCredentials` window (which would refresh on every call
  with 900 s credentials);
- a `needs-retry.s3` handler: on a 403 `AccessDenied` it calls `Provider.refresh()` once per request and retries with
  zero delay (the retry is re-signed with the new key). A failed refresh, or a second 403, surfaces the original error.

Nothing here logs or formats a credential.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import Credentials, ReadOnlyCredentials

from .credentials import Provider

REGION = "us-east-1"
_FLAG = "mist_reminted"


class _ProviderCredentials(Credentials):
    """botocore credentials that always read the provider (which caches and refreshes by P4)."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider
        self.method = "mist"
        self.account_id = None

    access_key = property(lambda s: s._provider.get().access_key_id, lambda s, v: None)
    secret_key = property(lambda s: s._provider.get().secret_access_key, lambda s, v: None)
    token = property(lambda s: s._provider.get().session_token, lambda s, v: None)

    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        c = self._provider.get()
        return ReadOnlyCredentials(c.access_key_id, c.secret_access_key, c.session_token)


class _Resolver:
    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials

    def load_credentials(self) -> Credentials:
        return self._credentials


def _remint_on_denied(provider: Provider):
    def handler(response=None, request_dict=None, **kwargs: Any) -> Optional[int]:
        if response is None or request_dict is None:
            return None
        http, parsed = response
        context = request_dict.setdefault("context", {})
        if http.status_code != 403 or context.get(_FLAG):
            return None
        # A HEAD 403 has no body: botocore reports the status as the code.
        if (parsed or {}).get("Error", {}).get("Code") not in ("AccessDenied", "403"):
            return None
        context[_FLAG] = True
        try:
            provider.refresh()
        except Exception:
            return None
        return 0

    return handler


def boto3_session(provider: Provider) -> boto3.Session:
    bs = botocore.session.Session()
    bs.register_component("credential_provider", _Resolver(_ProviderCredentials(provider)))
    bs.register("needs-retry.s3", _remint_on_denied(provider))
    return boto3.Session(botocore_session=bs, region_name=REGION)


def s3_client_kwargs(s3_endpoint: Optional[str], ca_bundle: Optional[str]) -> Dict[str, Any]:
    endpoint = s3_endpoint or os.environ.get("MIST_S3_ENDPOINT")
    if not endpoint:
        raise ValueError("no S3 endpoint: pass s3_endpoint= to Client or set MIST_S3_ENDPOINT")
    config = Config(
        retries={"mode": "standard", "max_attempts": 8},
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return {"endpoint_url": endpoint, "region_name": REGION, "verify": ca_bundle or None, "config": config}
