# mist-openai-agents: Mist for the OpenAI Agents SDK

Two things for an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) agent that uses
[Mist](../../README.md), the S3-compatible object store for agents:

| What | Use |
|---|---|
| `MistSession` | A session (conversation memory) stored in Mist as one JSON object, `<prefix><session_id>.json` |
| `mcp_server()` | An `MCPServerStdio` that starts the `mist-mcp` server with the Mist settings from the environment |

The Agents SDK has no skills loader; the `guide(topic)` MCP tool returns the Mist skills' text instead.

## Install

Python 3.10 or newer. Not on PyPI; install from GitHub:

```sh
pip install "mist-openai-agents @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/openai-agents"
```

`mcp_server()` starts the server with `uvx`, so install [uv](https://docs.astral.sh/uv/) too.

## Configure

| Variable | What |
|---|---|
| `MIST_ENDPOINT` | The gateway (STS and the Mist HTTP API), e.g. `https://mist.example.com:9200` |
| `MIST_S3_ENDPOINT` | The S3 endpoint, e.g. `https://mist.example.com:9000` |
| `MIST_IDENTITY_TOKEN_FILE` | A file holding the agent's identity token (JWT) |
| `MIST_CA_BUNDLE` | Only for a private CA |

`mcp_server()` passes these (and `PATH`, `HOME`) to the server, nothing else from your environment. The session
needs a bucket the agent owns, or a grant on the prefix.

## Use

```python
from agents import Agent, Runner
from mist_openai_agents import MistSession, mcp_server

async with mcp_server() as mist_tools:
    agent = Agent(name="worker", model=model, mcp_servers=[mist_tools])
    session = MistSession("conv-1", "my-agent", "sessions/")
    result = await Runner.run(agent, "Create a run workspace in my-agent for run 42", session=session)
```

A new `MistSession` with the same id resumes the conversation. Every `add_items` rewrites the object, so use one
session object per conversation at a time. `session_settings=SessionSettings(limit=50)` bounds what is sent back to
the model. The server timeout is 60 s so that the `feed` tool's 25 s long-poll fits.

The MCP tools are control plane only: they never move object data. For files, the agent calls `credentials` and
uses S3, or your code uses `mist.Client().s3()` (a boto3 client) directly.

## Example

`examples/openai_agent.py` creates a run workspace through the MCP tools, keeps the conversation in Mist, and lists
what landed in the bucket. It uses LM Studio at `http://localhost:1234/v1` with
`nvidia-nemotron-3.5-lightning-30b-a3b` through Chat Completions by default (`OPENAI_BASE_URL`, `MODEL`), and turns
tracing off (no OpenAI account is needed).

```sh
export MIST_ENDPOINT=... MIST_S3_ENDPOINT=... MIST_IDENTITY_TOKEN_FILE=... MIST_BUCKET=my-agent
python examples/openai_agent.py
```

## Tests

```sh
pip install -e ".[dev]" && pytest      # against an in-process fake S3 (moto); no network
```
