# How this repository is laid out

Mist is an S3-compatible object store for agents. This repository holds everything an agent harness needs to use it:
one shared core and a thin pack per harness. Keep it simple: V1.

## The core (written once)

| Folder | What | Install |
|---|---|---|
| `sdk/` | `mist-sdk`, the Python SDK (a copy of the SDK in the Mist repository, `sdk/python`) | `pip install "mist-sdk @ git+https://github.com/neonexia/mist-plugins#subdirectory=sdk"` |
| `mcp/` | `mist-mcp`, the MCP server: local, stdio, the agent's own identity. 19 control-plane tools plus `guide`. No object tools: data goes over S3 with the keys from the `credentials` tool. | `uvx --from "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp" mist-mcp` |
| `skills/` | The teaching kit as Agent Skills (`<name>/SKILL.md`): getting started, choosing MCP vs SDK vs S3, workspaces, sharing with grants and roles, change events, limits and errors | copied into each pack by `scripts/sync-skills.sh` |

## The packs (thin, one per harness)

| Folder | Harness | What is in it |
|---|---|---|
| `plugins/claude/` | Claude Code and the Claude Agent SDK | Claude Code plugin: MCP config, skills. Marketplace file at `.claude-plugin/marketplace.json` |
| `plugins/codex/` | OpenAI Codex | Codex plugin: MCP config, skills |
| `plugins/opencode/` | OpenCode | MCP config, skills |
| `plugins/hermes/` | Hermes Agent | MCP config, skills |
| `adapters/langchain/` | LangChain Deep Agents and LangGraph | `mist-langchain`: Deep Agents backend, LangGraph store and checkpointer on Mist |
| `adapters/strands/` | AWS Strands | `mist-strands`: session manager on Mist, skills and MCP wiring |
| `adapters/openai-agents/` | OpenAI Agents SDK | `mist-openai-agents`: session on Mist, MCP wiring |

## Configuration (every pack)

- `MIST_ENDPOINT`: the one Mist address (S3, STS at `/mist/v1/sts`, and the Mist HTTP API), for example `https://mist.example.com`.
- `MIST_S3_ENDPOINT`: only when S3 is on a different address; it defaults to `MIST_ENDPOINT`.
- `MIST_IDENTITY_TOKEN_FILE`: a file holding the agent's identity token (JWT), or a principal key (`MIST_PRINCIPAL_KEY_ID`, `MIST_PRINCIPAL_KEY_SECRET` if the SDK supports them from the environment).
- `MIST_CA_BUNDLE`: only for a private CA.

## Rules

- No secret, token or key is ever committed, logged, or returned by a tool except `credentials` (which returns the minted, short-lived S3 keys).
- Every pack is installed the way its harness documents for end users, from this public repository. No local paths, no hand-edited config.
- Not on PyPI or any registry yet.
