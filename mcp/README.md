# mist-mcp: the Mist MCP server

Mist's control plane as MCP tools, for agents that work through tool calls. It runs on the agent's machine, is
started by the harness over stdio, and acts as the agent's own identity. It never moves object data: the
`credentials` tool returns short-lived S3 keys, and data goes over plain S3.

Every pack in this repository (`plugins/`) starts this server for you. To run it by hand (needs
[uv](https://docs.astral.sh/uv/)):

```sh
uvx --from "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp" mist-mcp
```

## Configuration (environment)

| Variable | What |
|---|---|
| `MIST_ENDPOINT` | The gateway (STS and the Mist HTTP API), e.g. `https://mist.example.com:9200` |
| `MIST_S3_ENDPOINT` | The S3 endpoint, e.g. `https://mist.example.com:9000` (returned by `credentials`) |
| `MIST_IDENTITY_TOKEN_FILE` | A file holding the agent's identity token (JWT); re-read on every mint |
| `MIST_PRINCIPAL_KEY_ID`, `MIST_PRINCIPAL_KEY_SECRET` | Instead of a token file: a principal key |
| `MIST_CA_BUNDLE` | Only for a private CA |

The server starts without configuration; each tool then answers `NotConfigured` naming what is missing.

## Tools

| Group | Tools |
|---|---|
| Identity | `whoami`, `credentials` |
| Buckets | `create_bucket`, `get_bucket`, `delete_bucket` |
| Grants | `create_grant`, `list_grants`, `delete_grant` |
| Roles | `create_role`, `list_roles`, `delete_role` |
| Change events | `create_subscription`, `list_subscriptions`, `delete_subscription`, `feed` (a long-poll of at most 25 s) |
| Run workspaces | `create_workspace`, `list_workspaces` |
| Teaching | `guide(topic)`: the skills' text, for harnesses that do not load skills |

Tool names are the Mist HTTP API's operation ids. Read-only, destructive and idempotent hints are set. A failed call
returns Mist's error as JSON (`status`, `error`, `message`, `rule`) so the model can correct itself.

Subscriptions are feed subscriptions only (webhooks return a signing secret, which tools never return). The bucket
settings (versioning, quota, expiry, events switch) are in the Python SDK, not here.

**Timeouts.** `create_subscription` and `delete_subscription` can take up to about 90 s. Give the server a tool timeout
of at least 120 s where the harness has one.

## Development

```sh
cd mcp
uv venv && uv pip install -e ../sdk -e ".[dev]"
.venv/bin/pytest
```

The tests run the server against a fake Mist gateway (a local HTTP server), in process and over stdio.
`mist_mcp/skills/` is a copy of `../skills/` made by `scripts/sync-skills.sh`; do not edit it.
