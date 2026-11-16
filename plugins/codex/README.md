# Mist for OpenAI Codex

A Codex plugin: the Mist MCP server (`mist`, the control plane: identity, credentials, buckets, grants, roles, change
events, run workspaces) and six skills that teach Codex how to use Mist. Data goes over plain S3 with the keys from
the `credentials` tool.

## Before you install

1. [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH` (the plugin starts the MCP server
   with `uvx`), and git.
2. The AWS CLI, if Codex should read and write objects from the shell.
3. These variables in the environment Codex starts from (your shell profile). The plugin passes exactly these to the
   MCP server:

```sh
export MIST_ENDPOINT=https://mist.example.com        # the one Mist address: S3, STS and the API
export MIST_IDENTITY_TOKEN_FILE=$HOME/.mist/token.jwt     # your agent's identity token (JWT)
# or a principal key instead of the token file:
# export MIST_PRINCIPAL_KEY_ID=...  MIST_PRINCIPAL_KEY_SECRET=...
# export MIST_CA_BUNDLE=/path/to/ca.crt                   # only for a private CA
```

## Install

```sh
codex plugin marketplace add neonexia/mist-plugins
codex plugin add mist@mist-plugins
```

In the ChatGPT desktop app you can instead open the Plugins Directory, choose the `Mist` marketplace and install
`mist`.

Check: `codex mcp list` shows the `mist` server, enabled. Then ask Codex: "Who am I in Mist?" (it calls `whoami`).

Headless:

```sh
codex exec "Using the mist tools: create a run workspace in my bucket and list my workspaces."
```

Update: `codex plugin marketplace upgrade mist-plugins`. Remove: `codex plugin remove mist@mist-plugins`.

## Sandbox note

The `aws` CLI talks to the Mist S3 endpoint over the network. In Codex's default `workspace-write` sandbox network
access is off, so either approve the command when Codex asks, or allow network for the session
(`codex exec -c sandbox_workspace_write.network_access=true ...`). The MCP tools are not affected.

## What is in it

| Path | What |
|---|---|
| `.codex-plugin/plugin.json` | The plugin manifest |
| `.mcp.json` | Starts `mist-mcp` with `uvx` from this repository; passes the `MIST_*` variables; 150 s tool timeout (subscriptions can take about 90 s) |
| `skills/` | `mist-getting-started`, `mist-choosing-a-surface`, `mist-run-workspaces`, `mist-sharing-with-grants-and-roles`, `mist-change-events`, `mist-limits-and-errors` |

The marketplace file is at the repository root: `.agents/plugins/marketplace.json`.
