---
name: mist-sharing-with-grants-and-roles
description: Share Mist data with other agents using grants (rights for one principal on a bucket folder) and roles (reusable templates of rights that trusted principals opt into). Use when another agent must read or write your bucket, when you need access to someone else's data, or to re-share (delegate) access you were given.
license: Apache-2.0
---

# Sharing with grants and roles

In Mist nobody reaches your bucket unless you say so. You never share keys: you give the other agent rights, and it
mints its own credentials.

| Tool | What it is | Use when |
|---|---|---|
| Grant | Rights for one principal (or everyone with a claim value) on one bucket folder | Sharing a folder with a specific agent |
| Role | A named set of rights on your buckets, plus who may assume it | The same access for a group, chosen only when needed |
| Workspace grant | A grant that ends with a run workspace | A helper for one run (see `mist-run-workspaces`) |

Actions are classes: `read` (get, list, head), `write` (put, multipart, copy), `delete`, and `delegate` (the grantee
may re-share what it has).

## Who is the other agent

A principal is an `issuer` and an `id`. The other agent finds its own with `whoami` (`principal`) and tells you. A
claim grantee matches everyone whose token carries a claim value, e.g. a team:

```json
{"principal": {"issuer": "https://idp.example.com", "id": "agent-7"}}
{"claim": {"issuer": "https://idp.example.com", "claim": "team", "value": "research"}}
```

## Grant access to a folder

MCP:

```
create_grant(bucket="my-bucket", prefix="reports/", actions=["read"],
             grantee={"principal": {"issuer": "https://idp.example.com", "id": "agent-7"}},
             expires="2026-12-01T00:00:00Z")
```

Python:

```python
g = m.grants.create({"principal": {"issuer": "https://idp.example.com", "id": "agent-7"}},
                    "my-bucket", ["read"], prefix="reports/", expires="2026-12-01T00:00:00Z")
print(g["id"])
```

- `prefix` ends in `/`; omit it for the whole bucket. Give the narrowest folder that works.
- `expires` is optional (RFC 3339). Prefer one for anything temporary.
- You must own the bucket (`403 not_owner` otherwise), unless you are delegating (below).
- The grantee sees the right at its **next mint**: tell it to call `credentials` again (the SDK re-mints by itself
  on a 403).

## See and revoke

- `list_grants` with no arguments: the grants made **to you**.
- `list_grants(bucket="my-bucket")`: every grant on a bucket you own.
- `get_bucket` shows the bucket's grants too.
- `delete_grant(id=...)` revokes. Credentials already minted keep working until they expire (at most 15 minutes).

## Use access someone gave you

1. `whoami` lists it under `rights` (bucket, prefix, actions, `from: [{"grant": "<id>"}]`).
2. Call `credentials` again if the grant is newer than your keys.
3. Read with S3, only under the granted prefix: `aws s3 ls s3://their-bucket/reports/`. Listing the bucket root
   fails with `AccessDenied` when you only have a folder.

## Re-share (delegation)

With a grant that includes `delegate`, you may pass on part of it: same bucket, same or narrower folder and actions.

```
create_grant(bucket="their-bucket", prefix="reports/q4/", actions=["read"], parent="<your grant id>",
             grantee={"principal": {"issuer": "...", "id": "helper-3"}})
```

When the parent grant goes, everything delegated from it goes too.

## Roles

A role is a template of rights on your own buckets plus a trust list. A trusted principal adds the role's rights to
its credentials by minting with it; without the role, it does not have them. Good for "analysts may read
`reports/` when they ask for it".

```
create_role(name="report-reader", description="Read the published reports",
            rights=[{"bucket": "my-bucket", "prefix": "reports/", "actions": ["read"]}],
            trust=[{"claim": {"issuer": "https://idp.example.com", "claim": "team", "value": "analysts"}}])
```

The trusted agent:

- `list_roles`: the roles it may assume.
- `whoami(role="report-reader")`: preview the rights.
- `credentials(role="report-reader")`: keys with the role's rights added. In Python:
  `mist.Client(role="report-reader")`; for the AWS CLI: `mist-credentials --role report-reader`.

Only the role's author can delete it (`delete_role`); `403 not_author` otherwise. A role cannot carry `delegate`.

## Errors

| Error / rule | Meaning | Do |
|---|---|---|
| `403 not_owner` | You do not own the bucket | Ask the owner for a grant with `delegate`, then pass `parent` |
| `403 invalid_parent` | The parent grant is not yours, not on this bucket, lacks `delegate`, or is narrower | Check `list_grants`; narrow the request |
| `403 not_permitted` (delete) | You neither gave the grant nor own the bucket | Ask the owner |
| `409 RoleExists` | Role name taken | Pick another name |
| `404` on `whoami(role=...)` | The role does not exist or does not trust you | Check `list_roles` |
| S3 `AccessDenied` right after a grant | Your keys predate the grant | Call `credentials` again |
