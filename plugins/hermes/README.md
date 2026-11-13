# Mist for Hermes Agent

A Hermes portable plugin (Agent Plugins v1: `plugin.json`, `mcp.json`, `skills/`): the Mist MCP server (`mist`, the
control plane: identity, credentials, buckets, grants, roles, change events, run workspaces) and six skills that
teach the agent how to use Mist. Data goes over plain S3 with the keys from the `credentials` tool.

## Before you install

1. [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH` (the plugin starts the MCP server
   with `uvx`).
2. These variables in your shell or in your Hermes profile's `.env` (`~/.hermes/.env`). Hermes gives MCP servers a
   filtered environment; the plugin forwards exactly these:

```sh
MIST_ENDPOINT=https://mist.example.com:9200        # the gateway
MIST_S3_ENDPOINT=https://mist.example.com:9000     # S3
MIST_IDENTITY_TOKEN_FILE=/home/me/.mist/token.jwt  # your agent's identity token (JWT)
# or a principal key instead of the token file:
# MIST_PRINCIPAL_KEY_ID=...
# MIST_PRINCIPAL_KEY_SECRET=...
# MIST_CA_BUNDLE=/path/to/ca.crt                   # only for a private CA
```

## Install

```sh
hermes plugins install neonexia/mist-plugins/plugins/hermes --enable
```

(`--no-enable` installs it disabled; enable later with `hermes plugins enable mist`.)

Check: `hermes plugins list` shows `mist` enabled, and `hermes mcp list` shows the `mist` server. Then:

```sh
hermes chat -q "Who am I in Mist? Use the mist whoami tool."
```

The skills appear as `mist:mist-getting-started` and so on.

Update: `hermes plugins update mist`. Remove: `hermes plugins remove mist`.

## What is in it

| Path | What |
|---|---|
| `plugin.json` | The package manifest |
| `mcp.json` | Starts `mist-mcp` with `uvx` from this repository and forwards the `MIST_*` variables |
| `skills/` | `mist-getting-started`, `mist-choosing-a-surface`, `mist-run-workspaces`, `mist-sharing-with-grants-and-roles`, `mist-change-events`, `mist-limits-and-errors` |
