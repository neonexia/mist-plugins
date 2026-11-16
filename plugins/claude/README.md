# Mist for Claude Code and the Claude Agent SDK

A Claude Code plugin: the Mist MCP server (`mist`, the control plane: identity, credentials, buckets, grants, roles,
change events, run workspaces) and six skills that teach Claude how to use Mist. Data goes over plain S3 with the
keys from the `credentials` tool.

## Before you install

1. [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH` (the plugin starts the MCP server
   with `uvx`), and git.
2. The AWS CLI, if Claude should read and write objects from the shell.
3. These variables in the environment Claude Code starts from (your shell profile):

```sh
export MIST_ENDPOINT=https://mist.example.com        # the one Mist address: S3, STS and the API
export MIST_IDENTITY_TOKEN_FILE=$HOME/.mist/token.jwt     # your agent's identity token (JWT)
# or a principal key instead of the token file:
# export MIST_PRINCIPAL_KEY_ID=...  MIST_PRINCIPAL_KEY_SECRET=...
# export MIST_CA_BUNDLE=/path/to/ca.crt                   # only for a private CA
```

## Install in Claude Code

```sh
claude plugin marketplace add neonexia/mist-plugins
claude plugin install mist@mist-plugins
```

Or inside Claude Code: `/plugin marketplace add neonexia/mist-plugins`, then `/plugin install mist@mist-plugins`.
Restart Claude Code afterwards.

Check: `claude mcp list` shows `plugin:mist:mist ... Connected`. Then ask Claude: "Who am I in Mist?" (it calls
`whoami`). The first start downloads the server, which takes a few seconds.

Update: `claude plugin marketplace update mist-plugins`. Remove: `claude plugin uninstall mist@mist-plugins`.

## Use with the Claude Agent SDK

The Agent SDK loads the same plugin directory from a local path:

```sh
git clone --depth 1 https://github.com/neonexia/mist-plugins
pip install claude-agent-sdk
```

```python
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    plugins=[{"type": "local", "path": "./mist-plugins/plugins/claude"}],
    allowed_tools=["mcp__plugin_mist_mist__*", "Skill", "Bash(aws:*)"],
)

async def main():
    async for message in query(prompt="Who am I in Mist? Use the whoami tool.", options=options):
        print(message)

asyncio.run(main())
```

The MCP server reads the same `MIST_*` variables from the process environment.

## What is in it

| Path | What |
|---|---|
| `.claude-plugin/plugin.json` | The plugin manifest |
| `.mcp.json` | Starts `mist-mcp` with `uvx` from this repository |
| `skills/` | `mist-getting-started`, `mist-choosing-a-surface`, `mist-run-workspaces`, `mist-sharing-with-grants-and-roles`, `mist-change-events`, `mist-limits-and-errors` |

The marketplace file is at the repository root: `.claude-plugin/marketplace.json`.
