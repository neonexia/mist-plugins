---
name: mist-getting-started
description: Connect to Mist, the S3-compatible object store for agents. Use when you first need to store or read files in Mist, check who you are (whoami), get S3 credentials, or verify that Mist works from this machine.
license: Apache-2.0
---

# Getting started with Mist

Mist is an S3-compatible object store built for agents. You have your own identity (a principal), you own the
buckets you create, and other agents reach your data only through grants and roles you give them.

Three things to know:

1. **Data is plain S3.** Read, write, list and delete objects with any S3 client. Mist adds no object API.
2. **Credentials are short-lived.** Your identity (a JWT in a token file, or a principal key) is exchanged for
   S3 keys that last 15 minutes. The MCP `credentials` tool, the Python SDK and `mist-credentials` do this for you.
3. **Everything else is the Mist control plane**: buckets with owners, grants, roles, run workspaces, change events.
   As MCP tools, or as SDK calls.

## Configuration

Every Mist plugin reads the same environment variables:

| Variable | What |
|---|---|
| `MIST_ENDPOINT` | The gateway (STS and the Mist HTTP API), e.g. `https://mist.example.com:9200` |
| `MIST_S3_ENDPOINT` | The S3 endpoint, e.g. `https://mist.example.com:9000` |
| `MIST_IDENTITY_TOKEN_FILE` | A file holding your identity token (JWT). Re-read on every mint, so a rotated token just works |
| `MIST_PRINCIPAL_KEY_ID`, `MIST_PRINCIPAL_KEY_SECRET` | Instead of a token: a principal key from your Mist admin |
| `MIST_CA_BUNDLE` | Only when the cluster uses a private CA |

If a tool answers `NotConfigured` or `IdentityError`, one of these is missing. Ask the user to set it; never invent
a token.

## Step 1: who am I

MCP: call `whoami`. Python:

```python
import os, mist
m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))   # endpoints and token file from the environment
print(m.whoami())
```

The reply has `principal` (`issuer` and `id`: give these to another agent so it can grant you access),
`owned_buckets`, `rights` (bucket, folder, actions and where each right comes from) and `roles` you may assume.

A brand-new identity owns nothing and has no rights. That is normal. Create a bucket first (step 2): an identity with
no access at all cannot mint S3 credentials (`AccessDenied` from STS).

## Step 2: a bucket of your own

MCP: `create_bucket` with `name`. Python: `m.buckets.create("my-agent-data")`.

- Creating buckets needs a create permission from the cluster's admin, usually for a name pattern (for example
  `team-*`). `403` with rule `create_not_permitted` means exactly that: you have none for this name. Use a name that
  matches your permission, or ask the user for one, or work in a bucket someone has granted you.
- Names are global in the cluster: 3-63 characters, lowercase letters, digits, `-`. Pick something specific
  (`<agent>-<purpose>`). `409 BucketExists` means the name is taken: choose another.
- New buckets start with a whole-bucket expiry rule (30 days by default on a stock cluster): objects are deleted that
  many days after they were written. `get_bucket` shows the rules.

## Step 3: S3 credentials

MCP: call `credentials`. It returns `access_key_id`, `secret_access_key`, `session_token`, `expiration`,
`s3_endpoint`, and `env`, the same values as AWS environment variables. With the AWS CLI:

```sh
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=... AWS_REGION=us-east-1
export AWS_ENDPOINT_URL_S3="$MIST_S3_ENDPOINT"     # and AWS_CA_BUNDLE="$MIST_CA_BUNDLE" for a private CA
aws s3api put-object --bucket my-agent-data --key hello.txt --body ./hello.txt
aws s3api list-objects-v2 --bucket my-agent-data
```

In one shell command, pass the values inline (`AWS_ACCESS_KEY_ID=... aws s3api ...`) rather than writing them to a
file. Do not echo the secret or the session token back to the user.

Python (the SDK mints and refreshes for you, no copying keys):

```python
s3 = m.s3()                                   # a plain boto3 client
s3.put_object(Bucket="my-agent-data", Key="hello.txt", Body=b"hello")
print(s3.get_object(Bucket="my-agent-data", Key="hello.txt")["Body"].read())
```

AWS CLI without copying keys: install the SDK (`pip install "mist-sdk @ git+https://github.com/neonexia/mist-plugins#subdirectory=sdk"`)
and add a profile that runs `mist-credentials`:

```ini
# your AWS config file
[profile mist]
credential_process = mist-credentials
region = us-east-1
```

Then `aws --profile mist --endpoint-url "$MIST_S3_ENDPOINT" s3 ls s3://my-agent-data/`.

## Step 4: verify

1. `whoami` answers with your principal.
2. `credentials` answers with keys (needs at least one bucket or grant).
3. Put a small object, list it, get it back, delete it.

If all three work, Mist is ready. Next:

- `mist-choosing-a-surface`: MCP tools, the Python SDK or the AWS CLI, and when to use which.
- `mist-run-workspaces`: scratch space for one task that cleans itself up.
- `mist-sharing-with-grants-and-roles`: give another agent access.
- `mist-change-events`: react when objects change.
- `mist-limits-and-errors`: what each error means and what to do.

## Rules

- Your credentials are yours. Never hand your keys to another agent: grant it access instead.
- Keys expire in 15 minutes. On `ExpiredToken`, mint again (call `credentials`, or let the SDK refresh).
- A grant made to you after you minted shows up at the next mint: call `credentials` again.
