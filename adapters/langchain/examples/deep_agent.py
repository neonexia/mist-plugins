"""A Deep Agent whose files live in a Mist run workspace, with the Mist skills loaded.

Needs: pip install langchain-openai, the MIST_* settings (see the README), and a model behind an OpenAI-compatible
API (LM Studio by default: OPENAI_BASE_URL=http://localhost:1234/v1, MODEL=qwen/qwen3.8-27b).

The default model is Qwen, not Nemotron: Deep Agents' own `glob`, `grep` and `execute` tools have optional
parameters whose schemas say `"default": null`, and LM Studio's Nemotron prompt template rejects those (HTTP 400,
"Cannot apply filter "string" to type: NullValue").
"""

import os

import mist
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from mist_langchain import SKILLS_ROUTE, deep_agent_backend

BUCKET = os.environ.get("MIST_BUCKET", "deepagents-demo")

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
try:
    m.buckets.create(BUCKET)
except mist.Conflict:
    pass  # already there
ws = m.workspaces.create(BUCKET, ttl_days=1, run_id="deep-agent-demo")
print(f"workspace: s3://{BUCKET}/{ws['prefix']}")

model = ChatOpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "lm-studio"),
    model=os.environ.get("MODEL", "qwen/qwen3.8-27b"),
)
agent = create_deep_agent(
    model=model,
    backend=deep_agent_backend(BUCKET, ws["prefix"], client=m),
    skills=[SKILLS_ROUTE],
)
result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "Write a three-line note about what Mist is to /notes/mist.md, "
                "then list the files in /notes and tell me what is there.",
            }
        ]
    }
)
print("agent:", result["messages"][-1].content)

print("objects in Mist:")
for obj in m.s3().list_objects_v2(Bucket=BUCKET, Prefix=ws["prefix"]).get("Contents", []):
    print(f"  {obj['Key']}  {obj['Size']} bytes")
