# mist-langchain: Mist for Deep Agents and LangGraph

Keep a LangChain agent's files, checkpoints and long-term memory in [Mist](../../README.md), the S3-compatible
object store for agents, under the agent's own identity:

| Class | For | What it stores |
|---|---|---|
| `MistBackend` | Deep Agents (`create_deep_agent(backend=...)`) | The agent's files (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `delete`): `/notes/a.md` is the object `<prefix>notes/a.md` |
| `deep_agent_backend(...)` | Deep Agents | `MistBackend` plus the Mist skills, read-only, at `/skills/mist/` |
| `MistSaver` | LangGraph (`compile(checkpointer=...)`) | Checkpoints, so a thread resumes after a restart |
| `MistStore` | LangGraph (`compile(store=...)`) | Long-term memory items, one JSON object each |

Each is a few hundred lines over a plain boto3 client from `mist.Client().s3()`, so it uses the agent's own
short-lived Mist credentials and refreshes them by itself.

## Install

Python 3.11 or newer. Not on PyPI; install from GitHub:

```sh
pip install "mist-langchain @ git+https://github.com/neonexia/mist-plugins#subdirectory=adapters/langchain"
```

This also installs `mist-sdk`, `deepagents` and `langgraph`.

## Configure

The adapters build a `mist.Client` from these variables unless you pass `client=`:

| Variable | What |
|---|---|
| `MIST_ENDPOINT` | The gateway (STS and the Mist HTTP API), e.g. `https://mist.example.com:9200` |
| `MIST_S3_ENDPOINT` | The S3 endpoint, e.g. `https://mist.example.com:9000` |
| `MIST_IDENTITY_TOKEN_FILE` | A file holding the agent's identity token (JWT) |
| `MIST_CA_BUNDLE` | Only for a private CA |

The agent needs a bucket it owns, or a grant on the prefix you use. A principal with no access at all cannot get S3
credentials, so create the bucket first (`m.buckets.create("my-agent")`).

## Deep Agents

```python
import os
import mist
from deepagents import create_deep_agent
from mist_langchain import SKILLS_ROUTE, deep_agent_backend

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
ws = m.workspaces.create("my-agent", ttl_days=7, run_id="run-42")   # a run workspace: workspaces/7d/run-42/

agent = create_deep_agent(
    model=model,
    backend=deep_agent_backend("my-agent", ws["prefix"], client=m),
    skills=[SKILLS_ROUTE],                                          # the Mist skills
)
```

Use `MistBackend(bucket, prefix)` alone when you do not want the skills, or inside your own `CompositeBackend`
(for example routing `/memories/` to Mist and everything else to the default state backend).

`grep` reads every object under the searched path, so point it at a folder, not a bucket with millions of objects.

## LangGraph

```python
from mist_langchain import MistSaver, MistStore

graph = builder.compile(
    checkpointer=MistSaver("my-agent", "langgraph/checkpoints/"),
    store=MistStore("my-agent", "langgraph/store/"),
)
graph.invoke(inputs, {"configurable": {"thread_id": "thread-1"}})
```

Both work with `create_agent(..., checkpointer=..., store=...)` and with the async API (`ainvoke`, `astream`).
`MistStore` filters on values (`filter={"status": "done"}`, `$gt`, `$ne` and the other operators of LangGraph's
in-memory store); it has no vector index, so `search(query=...)` ignores the query. `MistSaver` keeps LangGraph's
own data model (checkpoints, channel blobs, pending writes) as objects; one writer per thread at a time.

## Examples

Both run against a live Mist and a model behind an OpenAI-compatible API, by default LM Studio at
`http://localhost:1234/v1` (`OPENAI_BASE_URL`, `MODEL` to change). `pip install langchain-openai` first.

- `examples/langgraph_agent.py`: a `create_agent` agent with a `remember` and a `recall` tool, its checkpoints and
  memory in Mist. Default model: `nvidia-nemotron-3.5-lightning-30b-a3b`.
- `examples/deep_agent.py`: a Deep Agent that writes a note into a run workspace and lists it. Default model:
  `qwen/qwen3.8-27b`, because LM Studio's Nemotron prompt template rejects Deep Agents' own `glob`, `grep` and
  `execute` tools (their optional parameters say `"default": null`; LM Studio answers 400 "Cannot apply filter
  "string" to type: NullValue").

```sh
export MIST_ENDPOINT=... MIST_S3_ENDPOINT=... MIST_IDENTITY_TOKEN_FILE=... MIST_BUCKET=my-agent
python examples/langgraph_agent.py
```

## Tests

```sh
pip install -e ".[dev]" && pytest      # against an in-process fake S3 (moto); no network
```
