import sqlite3

import pytest
from fastapi.testclient import TestClient

from orchestrator import Store
from orchestrator.dashboard import create_app


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def mk(store, **over):
    d = dict(title="T", assignee="research_manager", worker_role="w", context="c",
             acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"})
    d.update(over)
    return store.create_ticket(d, actor="top_manager")


def test_state_and_html(store, tmp_path):
    parent = mk(store)
    store.transition(parent["id"], "in_progress", "research_manager")
    child = store.create_ticket(dict(title="C", assignee="worker:x", worker_role="x", context="c",
                                     acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"}),
                                actor="research_manager", parent_id=parent["id"])
    store.transition(child["id"], "in_progress", "worker:x")
    esc = mk(store, title="Unsafe thing")
    store.transition(esc["id"], "in_progress", "research_manager")
    store.escalate(esc["id"], "research_manager", "safety", kind="safety")      # safety goes straight to the human
    store.register_resource("scope", "stop between sweeps")
    store.add_event(None, "w", "usage", {"input_tokens": 10, "output_tokens": 5})

    client = TestClient(create_app(tmp_path / "t.db", balance_fn=lambda: {}))
    s = client.get("/api/state").json()
    assert [n["title"] for n in s["needs_human"]] == ["Unsafe thing"]
    assert s["org"]["research_manager"][0]["worker"] == "worker:x"
    assert s["by_status"]["escalated"] == 1
    assert s["resources"][0]["safe_to_stop"] == "stop between sweeps"
    assert s["spend"]["input_tokens"] == 10 and s["spend"]["output_tokens"] == 5
    assert all(e["type"] != "usage" for e in s["events"])
    assert "Orchestrator" in client.get("/").text


def test_dashboard_is_read_only(store, tmp_path):
    from orchestrator.dashboard import _connect_ro
    c = _connect_ro(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM tickets")
    c.close()


def test_demo_seed_fills_every_panel(tmp_path):
    from orchestrator.dashboard import build_state, seed_demo
    from orchestrator.config import load_config
    seed_demo(tmp_path / "d.db")
    s = build_state(tmp_path / "d.db", load_config(None))
    kinds = {n["status"] for n in s["needs_human"]}
    assert kinds == {"escalated", "needs_clarification"}
    assert s["resources"] and s["jobs"] and s["org"] and s["events"] and s["spend"]["input_tokens"] == 120000
    assert s["by_status"]["done"] >= 2


def test_dashboard_shows_proposals_models_skipped_models_and_real_money(store, tmp_path):
    from orchestrator.config import load_config
    prop = mk(store, title="Build X for research", assignee="software_manager")
    store.escalate(prop["id"], "research_manager", "proposed by research_manager", kind="proposal", to="top_manager")
    hard = mk(store, title="Hard one")
    store.set_models(hard["id"], "top_manager", {"worker": "deepseek-v4-pro"})
    store.add_event(hard["id"], "w", "model_unavailable", {"model": "claude-cli:haiku", "why": "hit your 5-hour limit"})
    store.add_event(None, "w", "usage", {"model": "flash", "input_tokens": 1_000_000, "output_tokens": 0})
    cfg = load_config(None)
    cfg["prices"] = {"flash": {"input": 1.0, "output": 2.0}}
    cfg["budget"]["stop_below_balance_usd"] = 0.5
    client = TestClient(create_app(tmp_path / "t.db", cfg=cfg, balance_fn=lambda: {"deepseek": {"usd": 6.61}}))
    s = client.get("/api/state").json()
    assert any(h["kind"] == "proposal" and h["title"] == "Build X for research" for h in s["handling"])
    assert next(t for t in s["board"] if t["id"] == hard["id"])["models"] == {"worker": "deepseek-v4-pro"}
    assert s["unavailable_models"][0]["model"] == "claude-cli:haiku"
    assert s["spend"]["estimated_usd"] == pytest.approx(1.0)
    assert s["spend"]["balances"] == {"deepseek": {"usd": 6.61}} and s["spend"]["stop_below_balance_usd"] == 0.5
    html = client.get("/").text
    assert "Approve" in html and "skipped (quota)" in html and "balances" in html and "working" in html
