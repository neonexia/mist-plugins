"""An OpenAI Agents SDK agent with the mist-mcp tools and its conversation stored in Mist.

Needs: uv (for `uvx`, which starts mist-mcp), the MIST_* settings (see the README), and a model behind an
OpenAI-compatible Chat Completions API (LM Studio by default).
"""

import asyncio
import os

import mist
from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from openai import AsyncOpenAI

from mist_openai_agents import MistSession, mcp_server

BUCKET = os.environ.get("MIST_BUCKET", "openai-agents-demo")
set_tracing_disabled(True)  # no OpenAI platform account: keep traces local


async def main() -> None:
    m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))  # endpoints and token file from the environment
    try:
        m.buckets.create(BUCKET)
    except mist.Conflict:
        pass  # already there

    model = OpenAIChatCompletionsModel(
        model=os.environ.get("MODEL", "nvidia-nemotron-3.5-lightning-30b-a3b"),
        openai_client=AsyncOpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", "lm-studio"),
        ),
    )
    async with mcp_server() as mist_tools:
        agent = Agent(
            name="mist-demo",
            instructions="You use Mist through its tools. Call `guide` when unsure how Mist works.",
            model=model,
            mcp_servers=[mist_tools],
        )
        session = MistSession("openai-agents-demo", BUCKET, "sessions/", client=m)
        result = await Runner.run(
            agent,
            f"Create a Mist run workspace in bucket {BUCKET} with run id openai-agents-demo and a TTL of 1 day, "
            "then list the workspaces in that bucket and tell me the workspace prefix.",
            session=session,
        )
        print("agent:", result.final_output)

    print("objects in Mist:")
    for obj in m.s3().list_objects_v2(Bucket=BUCKET).get("Contents", []):
        print(f"  {obj['Key']}  {obj['Size']} bytes")


asyncio.run(main())
