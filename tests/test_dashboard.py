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
    store.transition(esc["id"], "escalated", "research_manager", note="safety")
    store.register_resource("scope", "stop between sweeps")
    store.add_event(None, "w", "usage", {"input_tokens": 10, "output_tokens": 5})

    client = TestClient(create_app(tmp_path / "t.db"))
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
