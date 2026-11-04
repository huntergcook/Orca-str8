import json

import pytest

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.dashboard import build_state, seed_demo
from orchestrator.manager import Manager
from orchestrator.providers import MockProvider


def j(**kw):
    return json.dumps(kw)


def spec(role):
    return dict(title=f"T-{role}", worker_role=role, context="c", acceptance_criteria=["a"],
                return_spec={"format": "f", "description": "d"})


def test_workers_are_numbered_per_role(tmp_path):
    store = Store(tmp_path / "t.db")
    p = MockProvider([j(action="delegate", tickets=[spec("surveyor"), spec("surveyor"), spec("analyst"),
                                                    spec("surveyor")])])
    m = Manager("research_manager", store, p, load_config(None), tmp_path, "role", ["m"], ["w"])
    top = store.create_ticket(dict(spec("worker"), assignee="research_manager"), "top_manager")["id"]
    d = m._decide("plan")                       # consumes the scripted delegate reply
    m._delegate(top, d)
    names = [c["assignee"] for c in store.list_tickets(parent_id=top)]
    assert names == ["worker:surveyor-1", "worker:surveyor-2", "worker:analyst-1", "worker:surveyor-3"]
    assert len(set(names)) == len(names)        # every worker distinguishable
    store.close()


def test_dashboard_lists_all_workers_of_open_tickets_including_finished(tmp_path):
    seed_demo(tmp_path / "d.db")
    s = build_state(tmp_path / "d.db", load_config(None))
    scouts = s["org"]["tooling_manager"]
    assert [w["worker"] for w in scouts] == ["worker:tool_scout-1", "worker:tool_scout-2", "worker:tool_scout-3",
                                                   "worker:tool_scout-4"]
    assert [w["status"] for w in scouts] == ["in_progress", "in_progress", "done", "escalated"]   # finished one stays visible
    assert all(w["role"] == "tool_scout" and w["ticket"] for w in scouts)
    # workers under a finished top-level ticket are hidden once nothing about them is active
    assert s["org"]["research_manager"] == []
