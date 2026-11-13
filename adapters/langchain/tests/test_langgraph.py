import asyncio
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore

from mist_langchain import MistSaver, MistStore


def test_store_put_get_search_delete(s3):
    store = MistStore("agent-files", "lg/store", s3=s3)
    store.put(("users", "u/1"), "prefs", {"theme": "dark", "n": 3})
    store.put(("users", "u2"), "prefs", {"theme": "light", "n": 5})
    store.put(("docs",), "a b", {"title": "x"})
    item = store.get(("users", "u/1"), "prefs")
    assert item.value == {"theme": "dark", "n": 3} and item.namespace == ("users", "u/1")
    created = item.created_at
    store.put(("users", "u/1"), "prefs", {"theme": "blue", "n": 4})
    assert store.get(("users", "u/1"), "prefs").created_at == created
    assert {i.namespace for i in store.search(("users",))} == {("users", "u/1"), ("users", "u2")}
    assert [i.value["theme"] for i in store.search(("users",), filter={"n": {"$gt": 4}})] == ["light"]
    assert store.list_namespaces() == [("docs",), ("users", "u/1"), ("users", "u2")]
    assert store.list_namespaces(prefix=("users",), max_depth=1) == [("users",)]
    store.delete(("docs",), "a b")
    assert store.get(("docs",), "a b") is None
    assert all(k["Key"].startswith("lg/store/") for k in s3.list_objects_v2(Bucket="agent-files")["Contents"])


def test_store_async(s3):
    store = MistStore("agent-files", s3=s3)

    async def go():
        await store.aput(("a",), "k", {"v": 1})
        return await store.aget(("a",), "k")

    assert asyncio.run(go()).value == {"v": 1}


class State(TypedDict):
    log: Annotated[list[str], operator.add]


def _graph(saver, store):
    def step(state: State, *, store: BaseStore):
        n = len(state["log"])
        store.put(("runs",), f"step-{n}", {"seen": n})
        return {"log": [f"step {n}"]}

    g = StateGraph(State)
    g.add_node("step", step)
    g.add_edge(START, "step")
    g.add_edge("step", END)
    return g.compile(checkpointer=saver, store=store)


def test_graph_resumes_from_checkpoints_in_mist(s3):
    store = MistStore("agent-files", "lg/store", s3=s3)
    cfg = {"configurable": {"thread_id": "thread/1"}}
    _graph(MistSaver("agent-files", "lg/ckpt", s3=s3), store).invoke({"log": ["start"]}, cfg)
    # A new saver object (as after a restart) sees the same thread.
    graph = _graph(MistSaver("agent-files", "lg/ckpt", s3=s3), store)
    out = graph.invoke({"log": ["again"]}, cfg)
    assert out["log"] == ["start", "step 1", "again", "step 3"]
    history = list(graph.get_state_history(cfg))
    assert len(history) >= 4
    assert store.get(("runs",), "step-3").value == {"seen": 3}
    saver = MistSaver("agent-files", "lg/ckpt", s3=s3)
    assert len(list(saver.list(cfg, limit=2))) == 2
    assert len(list(saver.list(None))) == len(history)
    saver.delete_thread("thread/1")
    assert saver.get_tuple(cfg) is None


def test_graph_async(s3):
    graph = _graph(MistSaver("agent-files", s3=s3), MistStore("agent-files", s3=s3))
    cfg = {"configurable": {"thread_id": "t2"}}
    out = asyncio.run(graph.ainvoke({"log": []}, cfg))
    assert out["log"] == ["step 0"]
    assert asyncio.run(graph.aget_state(cfg)).values["log"] == ["step 0"]


def test_pending_writes_survive(s3):
    saver = MistSaver("agent-files", s3=s3)
    from langgraph.checkpoint.base import empty_checkpoint

    cfg = saver.put({"configurable": {"thread_id": "t", "checkpoint_ns": ""}}, empty_checkpoint(), {}, {})
    saver.put_writes(cfg, [("a", 1), ("b", 2)], "task-1", "p")
    saver.put_writes(cfg, [("a", 9)], "task-1", "p")  # already stored: ignored
    assert saver.get_tuple(cfg).pending_writes == [("task-1", "a", 1), ("task-1", "b", 2)]
