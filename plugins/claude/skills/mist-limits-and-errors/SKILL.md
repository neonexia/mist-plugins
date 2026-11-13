---
name: mist-limits-and-errors
description: What Mist's errors and limits mean and how to recover - AccessDenied rules, expired credentials, quotas, expiry, conflicts and retryable 503s. Use when a Mist tool call, SDK call or S3 request fails, or before designing something that depends on lifetimes and sizes.
license: Apache-2.0
---

# Limits and errors

## How errors look

Mist HTTP errors (MCP tools, the SDK) are JSON:

```json
{"status": 403, "error": "AccessDenied", "message": "you do not own the bucket", "rule": "not_owner"}
```

`error` is the code, `rule` (on 403 and 409) says which rule refused it. **Read the rule and change the call.** Never
retry a 4xx unchanged. S3 errors are standard S3 XML codes (`AccessDenied`, `NoSuchKey`, `ExpiredToken`); the SDK
raises botocore `ClientError` for them.

## The rules

| `rule` | Means | Do |
|---|---|---|
| `not_owner` | The call needs the bucket's owner | Use your own bucket (`whoami` `owned_buckets`), or ask the owner for a grant |
| `not_author` | Only the role's author may change or delete it | Ask the author |
| `not_permitted` | You neither gave the grant nor own its bucket | Ask the owner |
| `invalid_parent` | A delegation's parent grant does not allow this | Narrow the request; check the parent has `delegate` |
| `not_readable` | You cannot read the whole folder you subscribe to | Subscribe to a folder you can read |
| `reserved_bucket` | The name is kept by Mist | Use another bucket name |
| `create_not_permitted` | No create permission covers this bucket name (an admin gives them, often per name pattern) | Use a name your permission covers, ask the user, or use a bucket shared with you |
| `bucket_not_empty` | `delete_bucket` on a bucket with objects | Delete the objects over S3 first |
| `run_id_taken` | A different workspace has this `run_id` | Choose another `run_id` |
| `subscription_limit` | Too many subscriptions | Delete unused ones |

## Codes without a rule

| Code | Means | Do |
|---|---|---|
| `BucketExists` (409) | The name is taken (names are cluster-wide) | Pick a more specific name |
| `NotFound` (404) | No such bucket, grant, role, subscription | Check the name or id; `whoami(role=...)` also 404s when the role does not trust you |
| `InvalidArgument` (400) | A field is malformed or out of range | Fix the field the message names |
| `CursorExpired` (410) | Feed cursor older than retention | Restart at `oldest_cursor` or `now_cursor` |
| `Unavailable` / `ServiceUnavailable` (503) | A node is busy or restarting | Wait `Retry-After` seconds (or 1-5 s) and repeat |
| `NotConfigured` (MCP) | `MIST_ENDPOINT` or the identity is not set | Ask the user to configure the plugin |
| `IdentityError` (MCP, SDK) | The token file is missing, empty or unreadable | Ask the user to fix `MIST_IDENTITY_TOKEN_FILE` |
| `NetworkError` | The gateway cannot be reached | Check `MIST_ENDPOINT`, the network, `MIST_CA_BUNDLE` |

## S3 errors you will meet

| S3 code | Means | Do |
|---|---|---|
| `AccessDenied` | No right for this key, or keys minted before a new grant | `whoami` to see rights; call `credentials` again; stay inside granted prefixes |
| `ExpiredToken` | Keys past their expiry (1 hour by default) | Mint again (`credentials`); the SDK does it for you |
| `NoSuchKey` | The object is gone (maybe expired by a TTL rule) | Expected with expiry and events; handle it |
| `PreconditionFailed` (412) | A conditional write (`If-None-Match: *`, `If-Match`) lost | Someone else wrote first: re-read and decide |
| `InvalidRequest` "Bucket quota exceeded" (400) | The bucket is full | Delete data, or ask the owner to raise the quota |
| `SlowDown` / 503 | Retryable | Back off and retry; boto3's standard retries do this |
| STS `AccessDenied` on mint | Your identity has no access at all yet | Create a bucket or get a grant first |

## Limits and lifetimes

| Thing | Value |
|---|---|
| S3 credentials | 1 hour by default (`sts.session_secs`); refresh before expiry. A presigned URL lives at most as long as its keys |
| New bucket expiry | A whole-bucket rule, 30 days by default (cluster setting), up to the cluster maximum (365 by default) |
| Workspace TTL | 1 day up to the shortest expiry rule covering the folder |
| Bucket names | 3-63 characters, lowercase, digits, `-`; unique in the cluster |
| Role names | a-z, 0-9, `-`, up to 64 |
| Quotas | Off unless the owner or admin sets one |
| Events feed | One read waits at most 25 s through MCP (55 s over HTTP); 100 events per batch by default, 500 max |
| Idle subscriptions | Removed after 7 days without a read (default) |
| Subscription create/delete | Can take up to about 90 s |
| Bucket creation over S3 | Denied: use `create_bucket` |
| Bucket policies | Denied: use grants and roles |
| Delete a bucket | Only when empty; delete objects first over S3 |

These are a stock cluster's defaults; an admin may change them. `get_bucket` shows a bucket's actual expiry rules
and quota.

## When something keeps failing

1. `whoami`: are you who you think, and do you have the right on that bucket and folder?
2. `credentials` again: new keys pick up new grants.
3. `get_bucket`: owner, grants, expiry, quota.
4. Report the `error`, `rule` and `request_id` to the user; the admin can find the request in the audit log by id.
