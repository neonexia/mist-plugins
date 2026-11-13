"""A Strands agent with the Mist skills, the mist-mcp tools, and its conversation stored in Mist.

Needs: pip install "strands-agents[openai]", uv (for `uvx`, which starts mist-mcp), the MIST_* settings (see the
README), and a model behind an OpenAI-compatible API (LM Studio by default).
"""

import os

import mist
from strands import Agent
from strands.models.openai import OpenAIModel

from mist_strands import MistSessionManager, mcp_client, skills_plugin

BUCKET = os.environ.get("MIST_BUCKET", "strands-demo")

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
try:
    m.buckets.create(BUCKET)
except mist.Conflict:
    pass  # already there

model = OpenAIModel(
    client_args={
        "base_url": os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1"),
        "api_key": os.environ.get("OPENAI_API_KEY", "lm-studio"),
    },
    model_id=os.environ.get("MODEL", "nvidia-nemotron-3.5-lightning-30b-a3b"),
)
agent = Agent(
    model=model,
    tools=[mcp_client()],  # the mist-mcp control-plane tools
    plugins=[skills_plugin()],  # the Mist skills
    session_manager=MistSessionManager("strands-demo", BUCKET, "strands/", client=m),
    callback_handler=None,
)
result = agent(
    f"Create a Mist run workspace in bucket {BUCKET} with run id strands-demo and a TTL of 1 day, "
    "then list the workspaces in that bucket and tell me the workspace prefix."
)
print("agent:", result)

print("objects in Mist:")
for obj in m.s3().list_objects_v2(Bucket=BUCKET).get("Contents", []):
    print(f"  {obj['Key']}  {obj['Size']} bytes")
