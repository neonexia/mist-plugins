import asyncio
import json

from agents import Agent, Runner, set_tracing_disabled
from agents.items import ModelResponse
from agents.memory import SessionSettings
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from mist_openai_agents import MCP_COMMAND, MistSession, mcp_server

set_tracing_disabled(True)


class EchoModel(Model):
    """Answers with how many input items it saw: no network."""

    def __init__(self):
        self.seen = []

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.seen.append(len(input) if isinstance(input, list) else 1)
        text = ResponseOutputText(type="output_text", text=f"seen {self.seen[-1]}", annotations=[])
        msg = ResponseOutputMessage(id="m", type="message", role="assistant", status="completed", content=[text])
        return ModelResponse(output=[msg], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def test_items_round_trip(s3):
    session = MistSession("conv-1", "agent-files", "sessions", s3=s3)

    async def go():
        assert await session.get_items() == []
        await session.add_items([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        await session.add_items([{"role": "user", "content": "c"}])
        assert [i["content"] for i in await session.get_items(limit=2)] == ["b", "c"]
        assert (await session.pop_item())["content"] == "c"
        stored = json.loads(s3.get_object(Bucket="agent-files", Key="sessions/conv-1.json")["Body"].read())
        assert [i["content"] for i in stored] == ["a", "b"]
        await session.clear_session()
        assert await session.get_items() == [] and await session.pop_item() is None

    asyncio.run(go())


def test_session_settings_limit(s3):
    session = MistSession("conv-2", "agent-files", s3=s3, session_settings=SessionSettings(limit=1))

    async def go():
        await session.add_items([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
        return await session.get_items()

    assert [i["content"] for i in asyncio.run(go())] == ["b"]


def test_runner_keeps_history_in_mist(s3):
    model = EchoModel()
    agent = Agent(name="a", model=model)

    async def go():
        await Runner.run(agent, "first", session=MistSession("conv-3", "agent-files", s3=s3))
        # A new session object (as after a restart) resumes the stored conversation.
        await Runner.run(agent, "second", session=MistSession("conv-3", "agent-files", s3=s3))

    asyncio.run(go())
    assert model.seen == [1, 3]


def test_mcp_server_passes_only_mist_env(monkeypatch):
    monkeypatch.setenv("MIST_ENDPOINT", "https://mist.example:9200")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    server = mcp_server()
    assert server.name == "mist"
    env = server.params.env
    assert env["MIST_ENDPOINT"] == "https://mist.example:9200" and "UNRELATED_SECRET" not in env
    assert server.params.command == MCP_COMMAND[0]
