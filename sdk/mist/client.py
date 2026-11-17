"""The typed Mist client (docs/design/sdk.md sections 3 and 5)."""
from __future__ import annotations

import copy
import os
import secrets
import ssl
import threading
from typing import Iterable, List, Optional, Union

import httpx

from ._generated import models
from ._http import Api, origin_of
from .credentials import Credentials, Provider, Timings, scope_policy
from .events import Events, _body
from .identity import resolve
from .s3 import boto3_session as _boto3_session
from .s3 import s3_client_kwargs as _s3_client_kwargs


class Buckets:
    def __init__(self, api: Api) -> None:
        self._api = api

    def create(self, name: str, expiry: Optional[list] = None, encryption: Optional[str] = None) -> models.NewBucket:
        """`encryption`: `"sse-s3"` encrypts new objects at rest (the cluster needs a key service), `"none"` does
        not; left out, the cluster's `bucket.default_encryption` decides (`none` unless the install changed it)."""
        body = _body(name=name, expiry=expiry, encryption=encryption)
        return self._api.call("create_bucket", body=body)  # type: ignore[return-value]

    def get(self, name: str) -> models.Bucket:
        return self._api.call("get_bucket", path={"name": name})  # type: ignore[return-value]

    def delete(self, name: str) -> models.Deleted:
        return self._api.call("delete_bucket", path={"name": name})  # type: ignore[return-value]

    def set_versioning(self, name: str, status: str, keep_versions: Optional[int] = None) -> models.Versioning:
        body = _body(status=status, keep_versions=keep_versions)
        return self._api.call("set_versioning", path={"name": name}, body=body)  # type: ignore[return-value]

    def set_encryption(self, name: str, encryption: str) -> models.BucketEncryption:
        """`"sse-s3"` or `"none"`. Applies to objects written afterwards; existing objects keep how they were stored."""
        body = {"encryption": encryption}
        return self._api.call("set_encryption", path={"name": name}, body=body)  # type: ignore[return-value]

    def set_quota(self, name: str, bytes: Optional[int]) -> models.QuotaSetting:
        """`bytes=None` means unlimited and is sent as `{"bytes": null}`."""
        return self._api.call("set_quota", path={"name": name}, body={"bytes": bytes})  # type: ignore[return-value]

    def set_expiry(self, name: str, prefix: str, days: int) -> models.ExpiryList:
        body = {"prefix": prefix, "days": days}
        return self._api.call("set_expiry", path={"name": name}, body=body)  # type: ignore[return-value]

    def remove_expiry(self, name: str, prefix: str) -> models.ExpiryList:
        return self._api.call("remove_expiry", path={"name": name}, query={"prefix": prefix})  # type: ignore[return-value]

    def set_events(self, name: str, enabled: bool) -> models.BucketEvents:
        body = {"enabled": enabled}
        return self._api.call("set_bucket_events", path={"name": name}, body=body)  # type: ignore[return-value]


class Grants:
    def __init__(self, api: Api) -> None:
        self._api = api

    def create(
        self,
        grantee: dict,
        bucket: str,
        actions: List[str],
        prefix: Optional[str] = None,
        expires: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> models.Grant:
        body = _body(grantee=grantee, bucket=bucket, actions=actions, prefix=prefix, expires=expires, parent=parent)
        return self._api.call("create_grant", body=body)  # type: ignore[return-value]

    def list(self, grantee: Optional[str] = None, bucket: Optional[str] = None) -> models.GrantList:
        """With neither argument, the grants made to the caller (`grantee="me"`)."""
        if grantee is None and bucket is None:
            grantee = "me"
        return self._api.call("list_grants", query={"grantee": grantee, "bucket": bucket})  # type: ignore[return-value]

    def delete(self, id: str) -> models.Deleted:
        return self._api.call("delete_grant", path={"id": id})  # type: ignore[return-value]


class Roles:
    def __init__(self, api: Api) -> None:
        self._api = api

    def create(self, name: str, rights: list, trust: list, description: Optional[str] = None) -> models.Role:
        body = _body(name=name, rights=rights, trust=trust, description=description)
        return self._api.call("create_role", body=body)  # type: ignore[return-value]

    def replace(self, name: str, rights: list, trust: list, description: Optional[str] = None) -> models.Role:
        body = _body(rights=rights, trust=trust, description=description)
        return self._api.call("replace_role", path={"name": name}, body=body)  # type: ignore[return-value]

    def get(self, name: str) -> models.Role:
        return self._api.call("get_role", path={"name": name})  # type: ignore[return-value]

    def delete(self, name: str) -> models.Deleted:
        return self._api.call("delete_role", path={"name": name})  # type: ignore[return-value]

    def assumable(self) -> models.RoleList:
        return self._api.call("list_roles", query={"assumable": "me"})  # type: ignore[return-value]


class Workspaces:
    """Run workspaces (docs/design/workspaces.md): a folder `workspaces/<ttl_days>d/<run_id>/` in a bucket the caller
    owns, with a TTL in whole days and an optional grant that ends with it, made by one server-side call."""

    def __init__(self, api: Api) -> None:
        self._api = api

    def create(
        self,
        bucket: str,
        ttl_days: int,
        run_id: Optional[str] = None,
        grantee: Optional[dict] = None,
        actions: Optional[List[str]] = None,
    ) -> models.Workspace:
        """Repeating the same call (same `run_id`) returns the same workspace. With no `run_id`, the SDK generates one
        (`run-` and 26 lowercase hex characters) before the first attempt and sends it, so its own retries of this call
        are idempotent too; read it from the reply. `actions` (default read, write, delete) apply only with a
        `grantee`. Objects expire `ttl_days` after each was written, so ones written near the end can live about
        2 x `ttl_days` from `created`."""
        if actions is not None and grantee is None:
            raise ValueError("actions need a grantee")
        if run_id is None:
            run_id = "run-" + secrets.token_hex(13)
        grant = _body(grantee=grantee, actions=actions) if grantee is not None else None
        body = _body(bucket=bucket, ttl_days=ttl_days, run_id=run_id, grant=grant)
        return self._api.call("create_workspace", body=body)  # type: ignore[return-value]

    def get(self, bucket: str, run_id: str) -> models.Workspace:
        return self._api.call("get_workspace", path={"bucket": bucket, "run_id": run_id})  # type: ignore[return-value]

    def list(self) -> models.WorkspaceList:
        """The workspaces in the buckets the caller owns."""
        return self._api.call("list_workspaces")  # type: ignore[return-value]

    def delete(self, bucket: str, run_id: str) -> models.Deleted:
        """Ends the workspace early: its grant goes at once; a background job then deletes its objects and its record."""
        return self._api.call("delete_workspace", path={"bucket": bucket, "run_id": run_id})  # type: ignore[return-value]


class Client:
    """The Mist client: typed HTTP calls and the credential provider behind `mist.s3()`.

    `with_scope()` narrows the S3 credentials only. The scoped client's Mist HTTP calls still authenticate with the
    full identity.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        s3_endpoint: Optional[str] = None,
        identity=None,
        role: Optional[str] = None,
        policy: Optional[dict] = None,
        ca_bundle: Optional[str] = None,
        session_name: str = "mist-sdk",
        timings: Timings = Timings(),
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        endpoint = endpoint or os.environ.get("MIST_ENDPOINT")
        if not endpoint:
            raise ValueError("no endpoint: pass endpoint= or set MIST_ENDPOINT")
        self.endpoint = origin_of(endpoint)
        self.s3_endpoint = s3_endpoint
        self.ca_bundle = ca_bundle
        self.role = role
        self.identity_source = resolve(identity)
        self._policy = copy.deepcopy(policy) if policy is not None else None
        self._timings = timings
        self._session_name = session_name
        self._owns_http = http_client is None
        if http_client is None:
            verify = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
            http_client = httpx.Client(verify=verify)
        self._http = http_client
        self._boto3_session = None
        self._session_lock = threading.Lock()
        self._api = Api(self.endpoint, self.identity_source, http_client, timings)
        self._provider = Provider(
            endpoint=self.endpoint,
            identity=self.identity_source,
            role=role,
            policy=self._policy,
            http=http_client,
            timings=timings,
            session_name=session_name,
        )
        self.buckets = Buckets(self._api)
        self.grants = Grants(self._api)
        self.roles = Roles(self._api)
        self.workspaces = Workspaces(self._api)
        self.events = Events(self._api)

    def whoami(self, role: Optional[str] = None) -> models.Whoami:
        """The principal, owned buckets and rights. `role` previews a mint with that role."""
        return self._api.call("whoami", query={"role": role})  # type: ignore[return-value]

    def credentials(self) -> Credentials:
        """The S3 credentials the provider holds now, minting (or refreshing) if needed."""
        return self._provider.get()

    def refresh(self) -> None:
        """Mint now, for example after a new grant."""
        self._provider.refresh()

    def boto3_session(self):
        """A `boto3.Session` whose credentials are this client's provider; the same session every call."""
        with self._session_lock:
            if self._boto3_session is None:
                self._boto3_session = _boto3_session(self._provider)
            return self._boto3_session

    def s3_client_kwargs(self) -> dict:
        """The settings for building an S3 client from `boto3_session()`: endpoint, region, verify and config."""
        return _s3_client_kwargs(self.s3_endpoint, self.ca_bundle, self.endpoint)

    def s3(self, **client_kwargs):
        """A stock boto3 S3 client over the refreshing credentials; `client_kwargs` override the defaults."""
        session = self.boto3_session()
        with self._session_lock:  # botocore's Session.create_client is not thread-safe
            return session.client("s3", **{**self.s3_client_kwargs(), **client_kwargs})

    def with_scope(
        self,
        *,
        read: Union[str, Iterable[str]] = (),
        write: Union[str, Iterable[str]] = (),
        delete: Union[str, Iterable[str]] = (),
        policy: Optional[dict] = None,
    ) -> "Client":
        """A new client whose S3 credentials are narrowed to `bucket/prefix` entries, or to a raw `policy`.

        A client that already has a policy cannot be scoped again: V1 does not intersect policies.
        """
        if self._policy is not None:
            raise ValueError("this client is already scoped; build the narrower scope from the unscoped client")
        helper = bool(read) or bool(write) or bool(delete)
        if policy is not None and helper:
            raise ValueError("pass read/write/delete or policy, not both")
        if policy is None:
            if not helper:
                raise ValueError("with_scope needs read, write, delete or policy")
            policy = scope_policy(read=read, write=write, delete=delete)
        return Client(
            self.endpoint,
            s3_endpoint=self.s3_endpoint,
            identity=self.identity_source,
            role=self.role,
            policy=policy,
            ca_bundle=self.ca_bundle,
            session_name=self._session_name,
            timings=self._timings,
            http_client=self._http,
        )

    def close(self) -> None:
        """Close the HTTP client when this client built it."""
        if self._owns_http:
            self._http.close()
