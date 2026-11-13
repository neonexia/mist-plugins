# mist-strands: Mist for AWS Strands Agents

Three things for a [Strands](https://strandsagents.com) agent that uses [Mist](../../README.md), the
S3-compatible object store for agents:

| What | Use |
|---|---|
| `MistSessionManager` | Strands' own `S3SessionManager` on Mist, with the agent's own Mist credentials: the conversation and agent state survive a restart |
| `skills_plugin()` | Strands' `AgentSkills` plugin loaded with the Mist skills (getting started, run workspaces, sharing, change events, limits and errors) |
| `mcp_client()` | A Strands `MCPClient` that starts the `mist-mcp` server (stdio) with the Mist settings from the environment |

## Install

Python 3.10 or newer. Not on PyPI; install from GitHub:

```sh
pip install "mist-strands @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/strands"
```

`mcp_client()` starts the server with `uvx`, so install [uv](https://docs.astral.sh/uv/) too. For an
OpenAI-compatible model (LM Studio, vLLM, OpenAI), also `pip install "strands-agents[openai]"`.

## Configure

| Variable | What |
|---|---|
| `MIST_ENDPOINT` | The gateway (STS and the Mist HTTP API), e.g. `https://mist.example.com:9200` |
| `MIST_S3_ENDPOINT` | The S3 endpoint, e.g. `https://mist.example.com:9000` |
| `MIST_IDENTITY_TOKEN_FILE` | A file holding the agent's identity token (JWT) |
| `MIST_CA_BUNDLE` | Only for a private CA |

`mcp_client()` passes these (and `PATH`, `HOME`) to the server, nothing else from your environment. The session
manager needs a bucket the agent owns, or a grant on the prefix.

## Use

```python
import os
import mist
from strands import Agent
from mist_strands import MistSessionManager, mcp_client, skills_plugin

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
agent = Agent(
    model=model,
    tools=[mcp_client()],                 # whoami, credentials, create_workspace, grants, events ... and guide
    plugins=[skills_plugin()],            # the Mist skills
    session_manager=MistSessionManager("chat-1", "my-agent", "strands/", client=m),
)
agent("Create a run workspace in my-agent for run 42")
```

The session lives at `s3://my-agent/strands/session_chat-1/` in Strands' own layout. A new agent with the same
session id resumes it.

The MCP tools are control plane only: they never move object data. For files, the agent calls `credentials` and
uses S3, or your code uses `mist.Client().s3()` (a boto3 client) directly.

## Example

`examples/strands_agent.py` creates a run workspace through the MCP tools, with the skills loaded and the
conversation stored in Mist, then lists what landed in the bucket. It uses LM Studio at
`http://localhost:1234/v1` with `nvidia-nemotron-3.5-lightning-30b-a3b` by default (`OPENAI_BASE_URL`, `MODEL`).

```sh
export MIST_ENDPOINT=... MIST_S3_ENDPOINT=... MIST_IDENTITY_TOKEN_FILE=... MIST_BUCKET=my-agent
python examples/strands_agent.py
```

## Tests

```sh
pip install -e ".[dev]" && pytest      # against an in-process fake S3 (moto); no network
```
