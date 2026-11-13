# Mist for OpenCode

The Mist MCP server (`mist`, the control plane: identity, credentials, buckets, grants, roles, change events, run
workspaces) and six skills that teach the agent how to use Mist, wired into OpenCode through its config file. Data
goes over plain S3 with the keys from the `credentials` tool.

OpenCode installs MCP servers and skills from its config (`opencode.json`); its code plugins are npm packages, which
Mist does not publish. So the install is two config entries: an MCP server started with `uvx` from this repository,
and a skills catalog that OpenCode downloads from this repository.

## Before you install

1. [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`.
2. The AWS CLI, if the agent should read and write objects from the shell.
3. These variables in the environment OpenCode starts from (your shell profile):

```sh
export MIST_ENDPOINT=https://mist.example.com:9200        # the gateway
export MIST_S3_ENDPOINT=https://mist.example.com:9000     # S3
export MIST_IDENTITY_TOKEN_FILE=$HOME/.mist/token.jwt     # your agent's identity token (JWT)
# or a principal key instead of the token file:
# export MIST_PRINCIPAL_KEY_ID=...  MIST_PRINCIPAL_KEY_SECRET=...
# export MIST_CA_BUNDLE=/path/to/ca.crt                   # only for a private CA
```

## Install

Add this to your OpenCode config: `~/.config/opencode/opencode.json` for every project, or `opencode.json` in one
project. If the file exists, merge the `mcp.servers.mist` entry and the `skills` URL into it.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "mist": {
        "type": "local",
        "command": ["uvx", "--from", "git+https://github.com/neonexia/mist-plugins#subdirectory=mcp", "mist-mcp"],
        "timeout": {"startup": 120000, "execution": 150000}
      }
    }
  },
  "skills": ["https://raw.githubusercontent.com/neonexia/mist-plugins/main/plugins/opencode/skills/"]
}
```

The same file is in this folder: `opencode.json`. For a fresh config you can download it:

```sh
mkdir -p ~/.config/opencode
curl -fsSL https://raw.githubusercontent.com/neonexia/mist-plugins/main/plugins/opencode/opencode.json \
  -o ~/.config/opencode/opencode.json      # only when you have no config there yet
```

Check: `opencode mcp list` shows `mist` connected. Then:

```sh
opencode run "Who am I in Mist? Use the mist whoami tool."
```

Skills are refreshed when the catalog's `version` changes. Remove: delete the two entries.

## What is in it

| Path | What |
|---|---|
| `opencode.json` | The config snippet above |
| `skills/` | The skills and `index.json`, the catalog OpenCode reads from the `skills` URL |
