"""A Deep Agents file backend on Mist: every file is an S3 object under one bucket and prefix."""

from __future__ import annotations

import base64
from typing import Any

from botocore.exceptions import ClientError
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    _get_backend_read_file_type,
    compile_grep_include_glob,
    grep_matches_from_files,
    perform_string_replacement,
    slice_read_response,
)

from ._s3 import delete, norm_prefix, s3_client


class MistBackend(BackendProtocol):
    """Deep Agents backend that keeps the agent's files in Mist.

    The agent path `/notes/a.md` is the object `<prefix>notes/a.md` in `bucket`. Credentials are the agent's own,
    from `mist.Client()` (the environment: `MIST_ENDPOINT`, `MIST_S3_ENDPOINT`, `MIST_IDENTITY_TOKEN_FILE`).

    Args:
        bucket: The bucket that holds the files. The agent must own it or hold a grant on the prefix.
        prefix: Key prefix for the files, for example a run workspace's `workspaces/7d/run-1/`.
        client: A `mist.Client`; built from the environment when omitted.
        s3: A boto3 S3 client to use instead of `client.s3()` (tests).
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: Any = None, s3: Any = None) -> None:
        self.bucket = bucket
        self.prefix = norm_prefix(prefix)
        self.s3 = s3 or s3_client(client)

    # -- paths and objects -------------------------------------------------------------------------------------

    def _key(self, path: str) -> str:
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"invalid path {path!r}: use an absolute path without '..'")
        return self.prefix + path.lstrip("/")

    def _path(self, key: str) -> str:
        return "/" + key[len(self.prefix) :]

    def _get(self, key: str) -> tuple[bytes, str] | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return obj["Body"].read(), obj["LastModified"].isoformat()

    def _put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def _objects(self, path: str | None) -> list[dict]:
        """Every object at or under `path`."""
        root = (path or "/").rstrip("/")
        if not root:
            prefix, keep = self.prefix, lambda k: True
        else:
            base = self._key(root)
            prefix, keep = base, lambda k: k == base or k.startswith(base + "/")
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return [o for page in pages for o in page.get("Contents", []) if keep(o["Key"])]

    @staticmethod
    def _file_data(data: bytes, modified: str) -> dict:
        try:
            content, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(data).decode("ascii"), "base64"
        return {"content": content, "encoding": encoding, "created_at": modified, "modified_at": modified}

    @staticmethod
    def _error(e: Exception) -> str:
        if isinstance(e, ClientError):
            err = e.response.get("Error", {})
            return f"Error: {err.get('Code')}: {err.get('Message', '')}".rstrip(": ")
        return f"Error: {e}"

    # -- BackendProtocol ---------------------------------------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        try:
            prefix = self._key(path).rstrip("/") + "/"
            if prefix == "/":
                prefix = self.prefix
            entries: list[FileInfo] = []
            pages = self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix, Delimiter="/"
            )
            for page in pages:
                for cp in page.get("CommonPrefixes", []):
                    entries.append({"path": self._path(cp["Prefix"]), "is_dir": True, "size": 0, "modified_at": ""})
                for o in page.get("Contents", []):
                    if o["Key"] != prefix:
                        entries.append(
                            {
                                "path": self._path(o["Key"]),
                                "is_dir": False,
                                "size": o["Size"],
                                "modified_at": o["LastModified"].isoformat(),
                            }
                        )
        except (ClientError, ValueError) as e:
            return LsResult(error=self._error(e))
        return LsResult(entries=sorted(entries, key=lambda x: x["path"]))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            got = self._get(self._key(file_path))
        except (ClientError, ValueError) as e:
            return ReadResult(error=self._error(e))
        if got is None:
            return ReadResult(error=f"File '{file_path}' not found")
        fd = self._file_data(*got)
        if _get_backend_read_file_type(file_path) != "text":
            return ReadResult(file_data=fd)
        return slice_read_response(fd, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            self._put(self._key(file_path), content.encode("utf-8"))
        except (ClientError, ValueError) as e:
            return WriteResult(error=self._error(e))
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        try:
            key = self._key(file_path)
            got = self._get(key)
            if got is None:
                return EditResult(error=f"Error: File '{file_path}' not found")
            try:
                text = got[0].decode("utf-8")
            except UnicodeDecodeError:
                return EditResult(error="Error: Cannot edit binary files with text replacements.")
            result = perform_string_replacement(text, old_string, new_string, replace_all)
            if isinstance(result, str):
                return EditResult(error=result)
            self._put(key, result[0].encode("utf-8"))
        except (ClientError, ValueError) as e:
            return EditResult(error=self._error(e))
        return EditResult(path=file_path, occurrences=int(result[1]))

    def delete(self, file_path: str) -> DeleteResult:
        try:
            base = self._key(file_path).rstrip("/")
            keys = [o["Key"] for o in self._objects(file_path) if o["Key"] == base or o["Key"].startswith(base + "/")]
            if not keys:
                return DeleteResult(error=f"Error: File '{file_path}' not found")
            delete(self.s3, self.bucket, keys)
        except (ClientError, ValueError) as e:
            return DeleteResult(error=self._error(e))
        return DeleteResult(path=file_path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None) -> GrepResult:
        try:
            files = {}
            for o in self._objects(path):
                got = self._get(o["Key"])
                if got is not None:
                    files[self._path(o["Key"])] = self._file_data(*got)
        except (ClientError, ValueError) as e:
            return GrepResult(error=self._error(e))
        return grep_matches_from_files(files, pattern, path or "/", glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        try:
            matcher = compile_grep_include_glob(pattern)
            root = (path or "/").rstrip("/")
            matches: list[FileInfo] = []
            for o in self._objects(path):
                p = self._path(o["Key"])
                rel = p.rsplit("/", 1)[-1] if p == root else p[len(root) + 1 :]
                if matcher(rel):
                    matches.append(
                        {"path": p, "is_dir": False, "size": o["Size"], "modified_at": o["LastModified"].isoformat()}
                    )
        except InvalidGlobPatternError as e:
            return GlobResult(error=str(e))
        except (ClientError, ValueError) as e:
            return GlobResult(error=self._error(e))
        matches.sort(key=lambda m: m["modified_at"], reverse=True)
        return GlobResult(matches=matches)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out = []
        for path, data in files:
            try:
                self._put(self._key(path), data)
                out.append(FileUploadResponse(path=path))
            except ValueError:
                out.append(FileUploadResponse(path=path, error="invalid_path"))
            except ClientError as e:
                out.append(FileUploadResponse(path=path, error=_op_error(e)))
        return out

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out = []
        for path in paths:
            try:
                got = self._get(self._key(path))
                if got is None:
                    out.append(FileDownloadResponse(path=path, error="file_not_found"))
                else:
                    out.append(FileDownloadResponse(path=path, content=got[0]))
            except ValueError:
                out.append(FileDownloadResponse(path=path, error="invalid_path"))
            except ClientError as e:
                out.append(FileDownloadResponse(path=path, error=_op_error(e)))
        return out


def _op_error(e: ClientError) -> str:
    code = e.response.get("Error", {}).get("Code", "")
    return "permission_denied" if code in ("AccessDenied", "403") else f"{code}: {e}"
