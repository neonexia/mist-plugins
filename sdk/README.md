# mist-sdk: the Python SDK for Mist

Data is S3, credentials are STS, and everything else Mist adds (buckets with owners, grants, roles, quotas, expiry,
events) is one Mist HTTP call. This SDK gets and refreshes your credentials, hands you a stock boto3 S3 client, and
gives you typed calls for the Mist HTTP API. It never wraps an object operation: you read and write with boto3.

```
pip install "mist-sdk @ git+https://github.com/neonexia/mist-plugins#subdirectory=sdk"   # import mist; Python 3.10+
```

(Not on PyPI yet. The PyPI name `mist` belongs to an unrelated project.)

## Two addresses

Mist listens on two addresses: the **gateway** (STS and the Mist HTTP API under `/mist/v1`) and the **S3** endpoint.

```python
import mist

m = mist.Client(
    endpoint="https://mist.example.com:9200",      # or MIST_ENDPOINT
    s3_endpoint="https://mist.example.com:9000",   # or MIST_S3_ENDPOINT
    ca_bundle="/etc/mist/ca.crt",                  # only for a private CA
)
```

## Identity

The SDK exchanges your identity for 15-minute S3 credentials and re-mints them before they expire. It looks in one
place on its own: the token file named by `MIST_IDENTITY_TOKEN_FILE`, else `AWS_WEB_IDENTITY_TOKEN_FILE` (re-read on
every mint, so a token your platform rotates just works). Anything else you pass:

| Source | Use it for |
|---|---|
| `mist.TokenFile(path)` | A file holding a JWT (a Kubernetes projected token). The default, from the environment |
| `mist.Token(jwt)` | A JWT string, for tests and short scripts. It cannot refresh past its own expiry |
| `mist.Callback(fn)` | Your function returns a fresh JWT each time the SDK asks (Okta, Entra, Auth0, GitHub Actions) |
| `mist.PrincipalKey(key_id, secret)` | A principal key from `mist principal-key create`, for agents with no identity provider |

GitHub Actions, with `permissions: id-token: write` on the job:

```python
import os
import httpx
import mist

def github_token():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=mist"
    auth = {"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}
    return httpx.get(url, headers=auth).json()["value"]

m = mist.Client(identity=mist.Callback(github_token))
```

`m.whoami()` shows who you are, the buckets you own and your rights. `role="analyst"` mints with that role's rights
added (`m.whoami(role="analyst")` previews them). `m.refresh()` mints now. A principal with no access at all (no
bucket, grant or role) cannot mint: STS answers `AccessDenied`, raised as `mist.AccessDenied`. The SDK never writes a token or a
credential to disk.

## S3: `m.s3()`

```python
s3 = m.s3()                                       # a plain boto3 S3 client
s3.put_object(Bucket="results", Key="run-1/out.json", Body=b"{}")
s3.get_object(Bucket="results", Key="run-1/out.json")["Body"].read()
```

The client has Mist's endpoint, path-style addressing, botocore's `standard` retries (8 attempts) and credentials
that refresh by themselves. A grant made after your credentials were minted reaches you at the next mint, so on a
`403 AccessDenied` the client re-mints once and retries the request; a second 403 is raised as usual. S3 errors stay
botocore `ClientError`.

`m.credentials()` returns the S3 credentials the client holds now, minting or refreshing if needed. `m.close()` closes the
HTTP client the SDK built (not one you passed in); closing a parent also closes the HTTP client its `with_scope`
children share.

**Presigned URLs** work as in boto3, but the URL is signed with your temporary credentials, so it works **at most 15
minutes, whatever `ExpiresIn` says**:

```python
url = s3.generate_presigned_url("get_object", Params={"Bucket": "results", "Key": "run-1/out.json"}, ExpiresIn=600)
```

**A library that builds its own client** (or takes a boto3 `Session`):

```python
session = m.boto3_session()                       # same refreshing credentials, same one-time re-mint on 403
s3 = session.client("s3", **m.s3_client_kwargs()) # endpoint, region, CA bundle and retry config
```

## Narrowing: `with_scope`

```python
reader = m.with_scope(read=["datasets/shared/"], write=["results/run-1/"])
reader.s3().get_object(Bucket="datasets", Key="shared/a.csv")    # allowed
```

`with_scope` sends a self-narrowing STS policy: it only removes rights. It narrows **the S3 credentials only**; the
scoped client's Mist HTTP calls still use your full identity. A scoped client cannot be scoped again; build the
narrower one from the unscoped client.

## The Mist HTTP API

```python
m.buckets.create("results")
m.buckets.set_versioning("results", "enabled", keep_versions=10)
m.buckets.set_quota("results", 10 * 2**30)        # None = unlimited
m.buckets.set_expiry("results", "tmp/", days=7)
m.grants.create({"principal": {"issuer": "https://idp.example.com", "id": "agent-7"}},
                "results", ["read"], prefix="run-1/", expires="2026-12-01T00:00:00Z")
m.roles.assumable()
```

A run workspace is one call: a folder `workspaces/<N>d/<run_id>/` in a bucket you own, a TTL in whole days and an
optional grant that ends with it. Repeating the call with the same `run_id` returns the same workspace.

```python
ws = m.workspaces.create("results", ttl_days=7, run_id="nightly-42",
                         grantee={"principal": {"issuer": "https://idp.example.com", "id": "agent-7"}})
ws["prefix"]                                      # "workspaces/7d/nightly-42/"
m.workspaces.delete("results", "nightly-42")      # ends access; objects go with the TTL rule
```

Replies are plain dicts (typed with `TypedDict`). Errors are `mist.MistError` with `status`, `error`, `message`,
`rule` and `request_id`, and the subclasses `AccessDenied`, `NotFound`, `Conflict`, `CursorExpired` and
`Unavailable`. Branch on `rule`:

```python
try:
    m.buckets.set_versioning("theirs", "enabled")
except mist.AccessDenied as e:
    print(e.rule)                                 # "not_owner"
```

## Events

```python
m.buckets.set_events("results", True)
sub = m.events.subscribe("results", "run-1/", name="watcher-1")      # a name makes it idempotent

events, cursor = m.events.wait(sub["id"], wait=25)                   # one long-poll
m.events.wait(sub["id"], after=cursor)                               # reading with after= acknowledges the batch

for ev in m.events.stream(sub["id"]):                                # wait() in a loop
    handle(ev)
```

Delivery is at least once. A read with no `after` starts from the subscription's stored position, so a restarted
process gets again whatever it had not acknowledged. `stream()` acknowledges a batch only when you ask for the next
event after it, and skips ids it saw recently. With `on_expired="oldest"` or `"now"` it restarts after a `410` and
yields a `mist.Gap` so you can re-list the prefix.

## Recipes

Coordination through conditional writes (`docs/recipes/coordination.md`), on any boto3 client:

```python
from mist import recipes
recipes.write_once(m.s3(), "results", "run-1/final", b"...")     # True: yours. False: someone published first
```

## `mist-credentials`: for the AWS CLI and other S3 tools

An AWS `credential_process` helper, installed with the package. It mints once, prints the credentials and exits.

```ini
# ~/.aws/config
[profile mist]
credential_process = mist-credentials --endpoint https://mist.example.com:9200 --ca-bundle /etc/mist/ca.crt

# narrowed: read datasets/shared/, write results/run-1/
[profile mist-run-1]
credential_process = mist-credentials --endpoint https://mist.example.com:9200 --read datasets/shared/ --write results/run-1/
```

```
aws --profile mist --endpoint-url https://mist.example.com:9000 s3api list-objects-v2 --bucket results
```

Identity, in order: `--token-file` when given; else `MIST_PRINCIPAL_KEY_ID` and `MIST_PRINCIPAL_KEY_SECRET` (both, or
neither); else the token file from `MIST_IDENTITY_TOKEN_FILE` or `AWS_WEB_IDENTITY_TOKEN_FILE`. `--role` picks a role;
`--read`, `--write` and `--delete` (repeatable, `bucket/prefix`) narrow the session like `with_scope`. On failure it
prints one line on stderr and exits 1.

