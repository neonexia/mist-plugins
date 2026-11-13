import json

from strands import Agent
from strands.models.model import Model

from mist_strands import MCP_COMMAND, SKILLS_DIR, MistSessionManager, mcp_client, skills_plugin


class EchoModel(Model):
    """A model that answers every turn with the text it was given: no network."""

    def __init__(self):
        self.calls = []

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return {}

    def structured_output(self, *args, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(len(messages))
        self.system_prompt = system_prompt
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": f"seen {len(messages)}"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket="agent-files").get("Contents", []))


def test_conversation_is_stored_in_mist_and_resumed(s3):
    agent = Agent(model=EchoModel(), session_manager=MistSessionManager("chat-1", "agent-files", "strands/", s3=s3))
    agent("hello")
    keys = _keys(s3)
    assert "strands/session_chat-1/session.json" in keys
    assert any(k.startswith("strands/session_chat-1/agents/") and "/messages/" in k for k in keys)

    # A fresh agent with the same session id (as after a restart) gets the history back.
    model = EchoModel()
    again = Agent(model=model, session_manager=MistSessionManager("chat-1", "agent-files", "strands/", s3=s3))
    assert len(again.messages) == 2
    again("and again")
    assert model.calls == [3]
    body = s3.get_object(Bucket="agent-files", Key="strands/session_chat-1/session.json")["Body"].read()
    assert json.loads(body)["session_id"] == "chat-1"


def test_delete_session(s3):
    sm = MistSessionManager("chat-2", "agent-files", s3=s3)
    Agent(model=EchoModel(), session_manager=sm)("hi")
    sm.delete_session("chat-2")
    assert _keys(s3) == []


def test_mcp_client_runs_mist_mcp_with_mist_env(monkeypatch):
    monkeypatch.setenv("MIST_ENDPOINT", "https://mist.example:9200")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    client = mcp_client()
    assert client is not None
    assert MCP_COMMAND[0] == "uvx" and MCP_COMMAND[-1] == "mist-mcp"


def test_skills_plugin_loads_the_mist_skills():
    assert SKILLS_DIR.is_dir(), "run scripts/sync-skills.sh"
    plugin = skills_plugin()
    model = EchoModel()
    agent = Agent(model=model, plugins=[plugin], callback_handler=None)
    names = [s.name for s in plugin.get_available_skills(agent)]
    assert "mist-getting-started" in names and all(n.startswith("mist-") for n in names)
    agent("hi")
    assert "mist-getting-started" in model.system_prompt
    # LM Studio's Nemotron template rejects tool schemas with a null default.
    assert '"default": null' not in json.dumps(agent.tool_registry.get_all_tool_specs())
