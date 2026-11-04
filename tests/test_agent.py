import pytest

from orchestrator import Store
from orchestrator.agent import parse_reply, run_ticket
from orchestrator.config import load_config, model_chain
from orchestrator.memory import Memory
from orchestrator.providers import Completion, MockProvider


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def mk(store):
    return store.create_ticket(dict(
        title="T", assignee="w1", worker_role="surveyor", context="ctx",
        acceptance_criteria=["a"], return_spec={"format": "summary", "description": "d"},
    ), actor="mgr")["id"]


def test_parse_reply_variants():
    assert parse_reply('```json\n{"action": "done", "result": {"x": "}"}}\n```')["result"] == {"x": "}"}
    assert parse_reply('blah {"nope": 1} then {"action": "ask", "question": "q"}')["action"] == "ask"
    assert parse_reply("no json") is None


def test_done(store):
    tid = mk(store)
    p = MockProvider(['{"action": "done", "result": {"summary": "ok"}}'])
    t = run_ticket(store, p, tid, "w1", "role", ["m1"])
    assert t["status"] == "done" and t["result"] == {"summary": "ok"}


def test_ask_then_resume(store):
    tid = mk(store)
    t = run_ticket(store, MockProvider(['{"action": "ask", "question": "which?"}']), tid, "w1", "r", ["m1"])
    assert t["status"] == "needs_clarification"
    store.answer_question(tid, "mgr", "this one")
    p = MockProvider(['{"action": "done", "result": {}}'])
    assert run_ticket(store, p, tid, "w1", "r", ["m1"])["status"] == "done"
    assert "this one" in p.calls[0]["messages"][0]["content"]


def test_capability_retry_advances_model_chain(store):
    tid = mk(store)
    p = MockProvider(["garbage", '{"action": "done", "result": {}}'])
    t = run_ticket(store, p, tid, "w1", "r", ["small", "big"])
    assert t["status"] == "done"
    assert [c["model"] for c in p.calls] == ["small", "big"]


def test_capability_exhaustion_escalates(store):
    tid = mk(store)
    p = MockProvider(["x", "y", "z"])
    assert run_ticket(store, p, tid, "w1", "r", ["m"], capability_retry_limit=3)["status"] == "escalated"


def test_refusal_retries_once_same_model_then_escalates(store):
    tid = mk(store)
    refusal = Completion(text="", refused=True, refusal_category="cyber", model="m1")
    p = MockProvider([refusal, refusal])
    t = run_ticket(store, p, tid, "w1", "r", ["m1", "other"])
    assert t["status"] == "escalated"
    assert [c["model"] for c in p.calls] == ["m1", "m1"]          # never rerouted
    assert "accurate" in p.calls[1]["messages"][-1]["content"]      # honest restatement
    types = [e["type"] for e in store.events(tid)]
    assert "refusal" in types and "refusal_escalated" in types


def test_refusal_then_success(store):
    tid = mk(store)
    p = MockProvider([Completion(text="", refused=True, model="m"), '{"action": "done", "result": {}}'])
    assert run_ticket(store, p, tid, "w1", "r", ["m"])["status"] == "done"


def test_usage_logged(store):
    tid = mk(store)
    run_ticket(store, MockProvider([Completion('{"action":"done"}', input_tokens=5, output_tokens=7, model="m")]),
               tid, "w1", "r", ["m"])
    u = [e for e in store.events(tid) if e["type"] == "usage"][0]["payload"]
    assert u["input_tokens"] == 5 and u["output_tokens"] == 7


def test_memory_roundtrip(tmp_path):
    m = Memory(tmp_path, "research")
    m.write_state("studying X")
    m.append_journal("did a thing")
    assert "studying X" in m.resume_brief() and "did a thing" in m.resume_brief()


def test_config_defaults_and_chain(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg["policies"]["max_subworker_depth"] == 2
    f = tmp_path / "c.yaml"
    f.write_text("models:\n  worker: {provider: anthropic, model: w}\n  fallback_chain: [{model: old}]\n")
    cfg = load_config(f)
    assert model_chain(cfg, "worker") == ["w", "old"]
    assert cfg["models"]["top"]["model"]            # defaults preserved by merge


def test_guarded_provider_pause_and_budget(store, tmp_path):
    from orchestrator.limits import GuardedProvider, Halted
    cfg = load_config(None)
    cfg["budget"]["max_tokens"] = 10
    inner = MockProvider(['{"action":"done"}'] * 3)
    g = GuardedProvider(inner, store, cfg, tmp_path)
    g.complete(model="m", system="s", messages=[])                  # fine: nothing used yet
    store.add_event(None, "x", "usage", {"input_tokens": 8, "output_tokens": 5})
    with pytest.raises(Halted, match="token budget"):
        g.complete(model="m", system="s", messages=[])
    cfg["budget"]["max_tokens"] = None
    g.complete(model="m", system="s", messages=[])                  # unlimited again
    (tmp_path / "PAUSE").write_text("")
    with pytest.raises(Halted, match="paused"):
        g.complete(model="m", system="s", messages=[])
    assert len(inner.calls) == 2
