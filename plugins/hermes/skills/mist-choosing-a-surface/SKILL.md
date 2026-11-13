---
name: mist-choosing-a-surface
description: Decide how to talk to Mist - MCP tools, the Python SDK (mist-sdk), or plain S3 with the AWS CLI or boto3 - and the SDK basics. Use when writing code or scripts against Mist, or when unsure which tool reads or writes data.
license: Apache-2.0
---

# Choosing a surface: MCP, SDK or S3

Mist has one data path and one control plane. Pick by what you are doing.

| You want to | Use |
|---|---|
| Read, write, list, delete objects | **S3**: the AWS CLI, boto3 (`m.s3()` in the SDK), or any S3 tool. Never MCP |
| Get S3 credentials | MCP `credentials` (interactive), the SDK (code), `mist-credentials` (AWS CLI profile) |
| Buckets, grants, roles, workspaces, events | MCP tools (interactive), or the SDK in code |
| Settings MCP does not have: versioning, quota, expiry rules, events switch | The SDK (`m.buckets.set_versioning`, `set_quota`, `set_expiry`, `set_events`) |
| A loop that runs for a long time (event consumer, pipeline) | The SDK: it refreshes credentials and reconnects for you |

Rule of thumb: **MCP for a few control-plane steps in a conversation; the SDK as soon as you write a program.**
The MCP tools never move object data. Large or many objects always go over S3.

## MCP tools (the `mist` server)

`whoami`, `credentials`, `create_bucket`, `get_bucket`, `delete_bucket`, `create_grant`, `list_grants`,
`delete_grant`, `create_role`, `list_roles`, `delete_role`, `create_subscription`, `list_subscriptions`,
`delete_subscription`, `feed`, `create_workspace`, `list_workspaces`, and `guide` (these how-tos).

Data with MCP: call `credentials`, then run the AWS CLI with the `env` values it returned:

```sh
AWS_ACCESS_KEY_ID=<access_key_id> AWS_SECRET_ACCESS_KEY=<secret_access_key> AWS_SESSION_TOKEN=<session_token> \
AWS_REGION=us-east-1 AWS_ENDPOINT_URL_S3=<s3_endpoint> \
  aws s3 cp ./report.md s3://my-bucket/reports/report.md
```

Add `AWS_CA_BUNDLE=<path>` when the cluster uses a private CA (the `env` object includes it when configured).

## The Python SDK (`mist-sdk`)

```sh
pip install "mist-sdk @ git+https://github.com/neonexia/mist-plugins#subdirectory=sdk"    # Python 3.10+
```

```python
import os
import mist

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))
# MIST_ENDPOINT, MIST_S3_ENDPOINT and MIST_IDENTITY_TOKEN_FILE come from the environment.
# A principal key instead: mist.Client(identity=mist.PrincipalKey(key_id, secret))

me = m.whoami()
m.buckets.create("my-bucket")

s3 = m.s3()                                    # a stock boto3 S3 client; credentials refresh by themselves
s3.put_object(Bucket="my-bucket", Key="a.txt", Body=b"hello")
for obj in s3.list_objects_v2(Bucket="my-bucket").get("Contents", []):
    print(obj["Key"], obj["Size"])
```

What the SDK gives you:

- `m.s3()`: boto3 with Mist's endpoint, path-style addressing, retries, and credentials that re-mint before expiry.
  On a `403` it re-mints once (a new grant) and retries.
- `m.buckets`, `m.grants`, `m.roles`, `m.workspaces`, `m.events`: the Mist HTTP API. Replies are plain dicts.
- `m.with_scope(read=["bucket/prefix/"], write=[...])`: a client whose S3 credentials are narrowed; hand it to a
  sub-task that should touch less.
- `m.events.wait()` / `m.events.stream()`: the change feed (see `mist-change-events`).
- `mist.recipes.write_once(s3, bucket, key, data)`: conditional-write coordination.

Errors are `mist.MistError` (`status`, `error`, `message`, `rule`), with subclasses `AccessDenied`, `NotFound`,
`Conflict`, `CursorExpired`, `Unavailable`. S3 errors stay botocore `ClientError`.

```python
try:
    m.buckets.delete("someone-elses")
except mist.AccessDenied as e:
    print(e.rule)            # "not_owner"
```

## Plain S3 tools (AWS CLI, rclone, DuckDB, pandas)

They only need credentials and the endpoint. The cleanest way is an AWS profile backed by `mist-credentials`
(installed with the SDK):

```ini
# your AWS config file
[profile mist]
credential_process = mist-credentials
region = us-east-1
s3 =
    addressing_style = path
```

```sh
aws --profile mist --endpoint-url "$MIST_S3_ENDPOINT" s3 ls s3://my-bucket/
```

`mist-credentials --read my-bucket/in/ --write my-bucket/out/` mints narrowed keys for a profile used by a tool you
trust less.

## Facts that hold on every surface

- Region `us-east-1`, path-style addressing.
- Credentials last 15 minutes; presigned URLs therefore work at most 15 minutes.
- Agents cannot create buckets over S3 (`CreateBucket` is denied): use `create_bucket`.
- Bucket policies (`PutBucketPolicy`) are denied: share with grants and roles.
