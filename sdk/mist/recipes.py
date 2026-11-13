"""Coordination recipes for agents sharing a Mist bucket. Needs boto3 >= 1.36 (IfMatch / IfNoneMatch on put_object).

Every function takes a boto3 S3 client. The recipes are published in docs/recipes/coordination.md, and the functional
end-to-end test (tests/e2e/scenarios/coordination.sh) runs exactly this code.
"""
import json
import random
import time

from botocore.exceptions import ClientError


def write_once(s3, bucket, key, body):
    """Publish `key` only if nobody has. True: yours. False: it already exists (HTTP 412)."""
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
            return False
        raise


def claim(s3, bucket, task_id, worker):
    """Claim a task. True: the task is yours (also if an earlier, timed-out attempt of yours landed).
    False: another worker claimed it."""
    key = "claims/" + task_id
    if write_once(s3, bucket, key, worker):
        return True
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode() == worker


def update_json(s3, bucket, key, change, max_tries=50):
    """Read-modify-write a JSON file with no lost update. `change(doc)` returns the new document ({} if the
    file does not exist yet). `change` must be idempotent: applying it to a document that already has the change
    must leave it unchanged, so that calling update_json again after a lost response is safe. Returns the number of tries."""
    for tries in range(1, max_tries + 1):
        try:
            r = s3.get_object(Bucket=bucket, Key=key)
            doc, cond = json.loads(r["Body"].read()), {"IfMatch": r["ETag"]}
        except ClientError as e:
            if e.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            doc, cond = {}, {"IfNoneMatch": "*"}
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(change(doc)).encode(), **cond)
            return tries
        except ClientError as e:
            # 412: someone changed the file since we read it. 404: it was deleted. Either way, read again.
            if e.response["ResponseMetadata"]["HTTPStatusCode"] not in (404, 412):
                raise
            time.sleep(random.uniform(0, min(0.5, 0.01 * 2 ** tries)))
    raise RuntimeError("gave up after %d tries" % max_tries)


def commit_pointer(s3, bucket, pointer_key, new_files):
    """Add already-written, never-overwritten data files to the table. Returns the new version number.
    Safe to call again after a lost response: files already listed are not added twice."""
    committed = {}

    def add(doc):
        files = doc.get("files", [])
        fresh = [f for f in new_files if f not in files]
        committed["v"] = doc.get("v", 0) + (1 if fresh else 0)
        return {"v": committed["v"], "files": files + fresh}

    update_json(s3, bucket, pointer_key, add)
    return committed["v"]


def read_snapshot(s3, bucket, pointer_key):
    """The files that make up the current state of the table ([] before the first commit)."""
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=pointer_key)["Body"].read())["files"]
    except ClientError as e:
        if e.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return []
        raise


def restore_version(s3, bucket, key, version_id):
    """Undo an overwrite on a versioned bucket: copy an old version over the current one. Returns the new VersionId."""
    r = s3.copy_object(Bucket=bucket, Key=key,
                       CopySource={"Bucket": bucket, "Key": key, "VersionId": version_id})
    return r.get("VersionId") or s3.head_object(Bucket=bucket, Key=key)["VersionId"]
