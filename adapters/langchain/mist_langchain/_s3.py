from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, unquote

from botocore.exceptions import ClientError


def s3_client(client: Any = None) -> Any:
    """A boto3 S3 client on the agent's own Mist credentials (`mist.Client()` from the environment by default)."""
    if client is None:
        import mist

        client = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))
    return client.s3()


def norm_prefix(prefix: str) -> str:
    return prefix.strip("/") + "/" if prefix.strip("/") else ""


def enc(part: str) -> str:
    """One path segment for any string: percent-encoded, never empty."""
    return "_" + quote(str(part), safe="")


def dec(seg: str) -> str:
    return unquote(seg[1:])


def get(s3: Any, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    return [o["Key"] for page in pages for o in page.get("Contents", [])]


def delete(s3: Any, bucket: str, keys_: list[str]) -> None:
    for i in range(0, len(keys_), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys_[i : i + 1000]], "Quiet": True})
