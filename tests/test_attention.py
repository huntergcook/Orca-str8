import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from orchestrator import Store, actions
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.dashboard import build_state, create_app
from orchestrator.manager import Manager
from orchestrator.providers import MockProvider

BASE = dict(worker_role="w", context="c", acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"})


def j(**kw):
    return json.dumps(kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "orchestrator.db")
    yield s
    s.close()


def escalated(store, title="T", parent=None, note="could not do it"):
    t = store.create_ticket({**BASE, "title": title, "assignee": "research_manager"}, "top_manager", parent_id=parent)
    store.transition(t["id"], "in_progress", "research_manager")
    store.transition(t["id"], "escalated", "research_manager", note=note)
    return t["id"]


def asking(store, question="which budget?"):
    t = store.create_ticket({**BASE, "title": "Q", "assignee": "research_manager"}, "top_manager")
    store.transition(t["id"], "in_progress", "research_manager")
    store.ask_question(t["id"], "research_manager", question)
    return t["id"]


# ------------------------------------------------------------------ actions

def test_actions_validate_state(store):
    q, e = asking(store), escalated(store)
    with pytest.raises(Exception, match="not waiting"):
        actions.answer(store, e, "x", "human")
    with pytest.raises(Exception, match="needs some text"):
        actions.answer(store, q, "  ", "human")
    with pytest.raises(Exception, match="only escalated"):
        actions.retry(store, q, "", "human")
    assert actions.answer(store, q, "$10", "human")["status"] == "in_progress"
    assert actions.retry(store, e, "use the 2020 data", "human")["status"] == "in_progress"
    assert "use the 2020 data" in store.get_ticket(e)["context"]
    with pytest.raises(Exception, match="nothing to cancel"):
        actions.cancel(store, e, "", "human")


def test_retrying_a_worker_reopens_its_escalated_parent_but_not_a_finished_one(store):
    parent = escalated(store, "parent")
    child = escalated(store, "child", parent=parent)
    actions.retry(store, child, "try again", "human")
    assert store.get_ticket(child)["status"] == "in_progress"
    assert store.get_ticket(parent)["status"] == "in_progress"          # reopened so the manager can resume

    done_parent = store.create_ticket({**BASE, "title": "dp", "assignee": "m"}, "top_manager")["id"]
    store.transition(done_parent, "in_progress", "m")
    kid = store.create_ticket({**BASE, "title": "k", "assignee": "w"}, "m", parent_id=done_parent)["id"]
    store.transition(kid, "in_progress", "w")
    store.transition(kid, "escalated", "w", note="x")
    store.transition(done_parent, "done", "m", result={})
    with pytest.raises(Exception, match="already done"):
        actions.retry(store, kid, "", "human")


def test_why_comes_from_the_right_event(store):
    t = escalated(store, note="temperature rising")
    assert actions.why_from_events(store.events(t)) == "temperature rising"
    store.add_event(t, "mgr", "run_error", {"error": "run error: RuntimeError: no key"})
    assert "no key" in actions.why_from_events(store.events(t))
    assert "model refusal (cyber)" in actions.why_from_events(
        [{"type": "refusal_escalated", "payload": {"category": "cyber"}}])


def test_manager_reruns_a_retried_worker(store, tmp_path):
    cfg = load_config(None)
    p = MockProvider([j(action="delegate", tickets=[dict(title="W", worker_role="r", context="c",
                                                         acceptance_criteria=["a"],
                                                         return_spec={"format": "f", "description": "d"})]),
                      j(action="escalate", reason="stuck"),                    # worker gives up
                      j(action="escalate", reason="worker stuck; need human"), # manager reviews, escalates
                      j(action="done", result={"fixed": True}),                # worker, after retry with guidance
                      j(action="done", result={"final": 1})])                  # manager review
    m = Manager("research_manager", store, p, cfg, tmp_path, "role", ["m"], ["w"])
    top = store.create_ticket({**BASE, "title": "Top", "assignee": "research_manager"}, "top_manager")["id"]
    assert m.work(top)["status"] == "escalated"
    kid = store.list_tickets(parent_id=top)[0]["id"]
    actions.retry(store, kid, "use the other approach", "human")               # reopens child + parent
    assert m.work(top)["status"] == "done"
    assert "use the other approach" in p.calls[3]["messages"][0]["content"]    # guidance reached the worker


# ---------------------------------------------------------------- ctl.watch

def make_ctl(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    return Ctl(cfg, tmp_path, MockProvider([]))


def test_watch_reports_new_attention_once_then_goes_idle(tmp_path):
    ctl = make_ctl(tmp_path)
    t = escalated(ctl.store, "Needs you", note="unsafe temperature")
    first = ctl.watch(timeout=5, grace=0.2, poll=0.05)
    assert first["reason"] == "attention"
    assert first["new_attention"][0]["id"] == t and "unsafe temperature" in first["new_attention"][0]["why"]
    second = ctl.watch(timeout=5, grace=0.2, poll=0.05)                         # already told: not reported again
    assert second["reason"] == "idle" and second["still_open"] == 1
    ctl.close()


def test_watch_wakes_for_an_item_that_appears_while_waiting(tmp_path):
    ctl = make_ctl(tmp_path)
    import os
    ctl.lock_path.write_text(str(os.getpid()))                                  # pretend a run is active

    def late():
        time.sleep(0.4)
        s = Store(tmp_path / "orchestrator.db")
        escalated(s, "Late escalation")
        s.close()
    threading.Thread(target=late).start()
    t0 = time.monotonic()
    out = ctl.watch(timeout=10, grace=5, poll=0.1)
    assert out["reason"] == "attention" and time.monotonic() - t0 < 5           # woke promptly, didn't time out
    ctl.close()


def test_watch_flags_pending_work_with_no_run_and_timeout(tmp_path):
    ctl = make_ctl(tmp_path)
    ctl.create(dict(department="research", title="T", context="c", acceptance_criteria=["a"],
                    return_spec={"format": "f", "description": "d"}))
    assert ctl.watch(timeout=5, grace=0.2, poll=0.05)["reason"] == "pending_no_run"
    import os
    ctl.lock_path.write_text(str(os.getpid()))
    assert ctl.watch(timeout=0.3, grace=5, poll=0.05)["reason"] == "timeout"
    ctl.close()


# ---------------------------------------------------------------- dashboard

def app_for(tmp_path, launched):
    db = tmp_path / "orchestrator.db"
    return TestClient(create_app(db, load_config(None), launch_run=lambda: launched.append(1),
                                 project=tmp_path, token="tok123"), base_url="http://127.0.0.1")


def test_dashboard_action_needs_token_and_localhost_host(store, tmp_path):
    e = escalated(store)
    launched = []
    c = app_for(tmp_path, launched)
    body = {"action": "retry", "ticket_id": e, "text": "go"}
    assert c.post("/api/action", json=body).status_code == 403                         # no token
    assert c.post("/api/action", json=body, headers={"x-dashboard-token": "wrong"}).status_code == 403
    evil = c.post("/api/action", json=body, headers={"x-dashboard-token": "tok123", "host": "evil.example.com"})
    assert evil.status_code == 403
    assert store.get_ticket(e)["status"] == "escalated" and launched == []             # nothing changed
    ok = c.post("/api/action", json=body, headers={"x-dashboard-token": "tok123"})
    assert ok.status_code == 200 and ok.json()["status"] == "in_progress" and ok.json()["run_started"] is True
    assert launched == [1]
    assert "tok123" in c.get("/").text and "__DASH_TOKEN__" not in c.get("/").text    # token only in the served page


def test_dashboard_answer_cancel_errors_and_no_double_launch(store, tmp_path):
    q, e = asking(store), escalated(store)
    launched = []
    c = app_for(tmp_path, launched)
    h = {"x-dashboard-token": "tok123"}
    assert c.post("/api/action", json={"action": "answer", "ticket_id": q, "text": "$10"}, headers=h).json()["status"] == "in_progress"
    bad = c.post("/api/action", json={"action": "answer", "ticket_id": e, "text": "x"}, headers=h)
    assert bad.status_code == 400 and "not waiting" in bad.json()["error"]
    assert c.post("/api/action", json={"action": "explode", "ticket_id": e}, headers=h).status_code == 400
    import os
    (tmp_path / "RUNNING").write_text(str(os.getpid()))                                # a run is already active
    cancelled = c.post("/api/action", json={"action": "cancel", "ticket_id": e, "text": "not needed"}, headers=h)
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["run_started"] is False
    assert len(launched) == 1                                                          # only the first answer launched
    assert [a for a in actions.ACTIONS] == ["answer", "retry", "cancel"]


def test_dashboard_state_has_why_level_and_run_flag(store, tmp_path):
    e = escalated(store, note="sensor offline")
    q = asking(store, "what rate?")
    store.handoff(e, "top_manager", "sensor is offline; only you can power-cycle it")
    store.handoff(q, "top_manager")
    s = TestClient(create_app(tmp_path / "orchestrator.db", load_config(None), project=tmp_path),
                   base_url="http://127.0.0.1").get("/api/state").json()
    by = {n["id"]: n for n in s["needs_human"]}
    assert "power-cycle" in by[e]["why"] and by[e]["level"] == "top" and by[e]["route"] == "human"
    assert by[q]["detail"] == "what rate?" and s["run_active"] is False
    assert s["handling"] == []
