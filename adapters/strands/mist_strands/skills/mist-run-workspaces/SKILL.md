---
name: mist-run-workspaces
description: Create a Mist run workspace - a folder for one task or run that expires by itself after N days, optionally shared with one other agent. Use for scratch space, intermediate results, hand-offs between agents, or any data that should not outlive the run.
license: Apache-2.0
---

# Run workspaces

A run workspace is a folder `workspaces/<ttl_days>d/<run_id>/` in a bucket you own. Objects in it are deleted
`ttl_days` after each was written, and an optional grant to one other agent ends with it. One call makes it all.

Use one when a task needs scratch space, a place for intermediate results, or a hand-off folder for a helper agent.
You do not have to clean it up.

## Create one

You need a bucket you own (`whoami` lists `owned_buckets`; else `create_bucket`).

MCP:

```
create_workspace(bucket="my-bucket", ttl_days=3, run_id="summarize-2026-11-13")
```

Python:

```python
ws = m.workspaces.create("my-bucket", ttl_days=3, run_id="summarize-2026-11-13")
prefix = ws["prefix"]          # "workspaces/3d/summarize-2026-11-13/"
```

The reply: `bucket`, `run_id`, `prefix`, `ttl_days`, `created`, `expires`, `state` (`ready`), `grant`.

- **Give a `run_id`** you can derive again (task name plus date, a ticket id). Repeating the same call returns the
  same workspace, so a retry or a restarted agent never makes a second one. Omit it and one is generated: read it from
  the reply.
- `run_id`: letters, digits, `_` and `-`, up to 64.
- `ttl_days`: whole days, at least 1, and no more than the bucket's expiry rule that covers the folder (a new bucket
  has a whole-bucket rule, 30 days on a stock cluster) and the cluster's maximum (365 by default).

## Write and read in it

Everything under `prefix` is ordinary S3. With credentials from the `credentials` tool:

```sh
aws s3 cp ./notes.md "s3://my-bucket/workspaces/3d/summarize-2026-11-13/notes.md"
aws s3 ls "s3://my-bucket/workspaces/3d/summarize-2026-11-13/"
```

Python:

```python
s3 = m.s3()
s3.put_object(Bucket=ws["bucket"], Key=prefix + "notes.md", Body=b"# Notes\n")
keys = [o["Key"] for o in s3.list_objects_v2(Bucket=ws["bucket"], Prefix=prefix).get("Contents", [])]
```

(Set the `AWS_*` variables from `credentials` first, as in `mist-getting-started`.)

## Share it with a helper agent

Pass the helper's principal (it gets it from its own `whoami`):

```
create_workspace(bucket="my-bucket", ttl_days=1, run_id="review-42",
                 grantee={"principal": {"issuer": "<issuer>", "id": "<helper id>"}},
                 actions=["read", "write"])
```

The helper calls `credentials` again (a grant appears at the next mint) and reads and writes under the prefix, and
nowhere else in your bucket. Default actions are read, write and delete. The grant ends at `expires`.

## List and end

- `list_workspaces` lists the workspaces in every bucket you own.
- To end one early, use the SDK: `m.workspaces.delete(bucket, run_id)`. Access ends at once; Mist then deletes
  the workspace's objects and its record in the background. The run id stays taken until that finishes.

## Good habits

- One workspace per run, not per file.
- Keep the final result outside `workspaces/` (for example `results/<run_id>/`) if it must outlive the run.
- Objects written near the end can live up to about 2 x `ttl_days` after `created`; do not rely on deletion for
  secrecy, only for cleanup.

## Errors

| Error / rule | Meaning | Do |
|---|---|---|
| `403 not_owner` | Not your bucket | Use a bucket from `owned_buckets`, or create one |
| `404 NotFound` | No such bucket | Check the name |
| `409 WorkspaceExists` / `run_id_taken` | That `run_id` exists with other settings (TTL, grant) | Pick another `run_id`, or repeat the original call exactly |
| `400 InvalidArgument` | `ttl_days` too long for the bucket's rules, bad `run_id` or grantee | Lower `ttl_days` (see `get_bucket` expiry), fix the field |
| `400 TooManyLifecycleRules` | The bucket has 1,000 lifecycle rules | Reuse an existing `ttl_days` value |
