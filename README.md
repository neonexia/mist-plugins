# Mist plugins

Plugins, an MCP server and adapters that let AI agents use [Mist](https://github.com/neonexia), an S3-compatible
object store built for agents: per-agent identity, short-lived S3 credentials, grants and roles, run workspaces and
change events.

## Install

| Harness | Install | Guide |
|---|---|---|
| Claude Code | `claude plugin marketplace add neonexia/mist-plugins` then `claude plugin install mist@mist-plugins` | [plugins/claude](plugins/claude/README.md) |
| Claude Agent SDK | the same plugin directory, loaded with `plugins=[{"type": "local", ...}]` | [plugins/claude](plugins/claude/README.md#use-with-the-claude-agent-sdk) |
| OpenAI Codex | `codex plugin marketplace add neonexia/mist-plugins` then `codex plugin add mist@mist-plugins` | [plugins/codex](plugins/codex/README.md) |
| OpenCode | two entries in `opencode.json` (the MCP server and a skills URL) | [plugins/opencode](plugins/opencode/README.md) |
| Hermes Agent | `hermes plugins install neonexia/mist-plugins/plugins/hermes --enable` | [plugins/hermes](plugins/hermes/README.md) |
| LangChain Deep Agents, LangGraph | `pip install "mist-langchain @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/langchain"` | [adapters/langchain](adapters/langchain/README.md) |
| AWS Strands | `pip install "mist-strands @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/strands"` | [adapters/strands](adapters/strands/README.md) |
| OpenAI Agents SDK | `pip install "mist-openai-agents @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/openai-agents"` | [adapters/openai-agents](adapters/openai-agents/README.md) |
| Any MCP client | `uvx --from "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp" mist-mcp` | [mcp](mcp/README.md) |
| Python code | `pip install "mist-sdk @ git+https://github.com/neonexia/mist-plugins#subdirectory=sdk"` | [sdk](sdk/README.md) |

Every pack reads the same settings from the environment: `MIST_ENDPOINT` (the one Mist address), `MIST_S3_ENDPOINT` (only if S3 is elsewhere),
`MIST_IDENTITY_TOKEN_FILE` (the agent's identity token) or `MIST_PRINCIPAL_KEY_ID` and `MIST_PRINCIPAL_KEY_SECRET`,
and `MIST_CA_BUNDLE` for a private CA. The packs that start the MCP server need [uv](https://docs.astral.sh/uv/).

Nothing here is on PyPI or a plugin registry yet: everything installs from this repository. Layout: `DESIGN.md`.
The skills in `skills/` are shared by every pack; `scripts/sync-skills.sh` copies them into the packs and `--check`
verifies the copies.

Status: V1 preview. License: Apache-2.0.
