"""A LangGraph agent whose checkpoints and long-term memory (store) live in Mist.

Run it twice: the second run resumes the same thread from Mist and still sees the saved fact.
Needs: pip install langchain-openai, the MIST_* settings (see the README), and a model behind an OpenAI-compatible
API (LM Studio by default).
"""

import os

import mist
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_openai import ChatOpenAI

from mist_langchain import MistSaver, MistStore

BUCKET = os.environ.get("MIST_BUCKET", "langgraph-demo")

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
try:
    m.buckets.create(BUCKET)
except mist.Conflict:
    pass  # already there


@tool
def remember(fact: str, runtime: ToolRuntime) -> str:
    """Save a fact to long-term memory."""
    facts = runtime.store.search(("facts",))
    runtime.store.put(("facts",), f"fact-{len(facts) + 1}", {"text": fact})
    return "saved"


@tool
def recall(runtime: ToolRuntime) -> list[str]:
    """List every fact in long-term memory."""
    return [item.value["text"] for item in runtime.store.search(("facts",), limit=100)]


model = ChatOpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "lm-studio"),
    model=os.environ.get("MODEL", "nvidia-nemotron-3.5-lightning-30b-a3b"),
)
agent = create_agent(
    model,
    tools=[remember, recall],
    checkpointer=MistSaver(BUCKET, "langgraph/checkpoints/", client=m),
    store=MistStore(BUCKET, "langgraph/store/", client=m),
)
config = {"configurable": {"thread_id": "demo-thread"}}
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Remember that the build server is called kestrel. Then recall all facts."}]},
    config,
)
print("agent:", result["messages"][-1].content)
print("messages in this thread:", len(agent.get_state(config).values["messages"]))

print("objects in Mist:")
for obj in m.s3().list_objects_v2(Bucket=BUCKET, Prefix="langgraph/").get("Contents", []):
    print(f"  {obj['Key']}  {obj['Size']} bytes")
