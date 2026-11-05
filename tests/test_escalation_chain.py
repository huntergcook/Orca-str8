"""Escalations climb worker -> manager -> director -> human; only unresolvable items reach the human."""
import json

import pytest

from orchestrator import Store, actions
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.manager import Manager
from orchestrator.providers import Completion, MockProvider
from orchestrator.store import route_for

BASE = dict(worker_role="r", context="c", acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"})
WORKER = dict(title="W", worker_role="r", context="c", acceptance_criteria=["a"],
              return_spec={"format": "f", "description": "d"})
SPEC = dict(department="research", title="Survey", context="c", acceptance_criteria=["a"],
            return_spec={"format": "f", "description": "d"})


def j(**kw):
    return json.dumps(kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def mk_manager(store, tmp_path, script, worker_models=("flash", "pro")):
    p = MockProvider(script)
    return Manager("research_manager", store, p, load_config(None), tmp_path, "role", ["m"], list(worker_models)), p


def top_ticket(store):
    return store.create_ticket({**BASE, "title": "Top", "assignee": "research_manager"}, "director")["id"]


# ------------------------------------------------------------------ routing

def test_routing_rules(store):
    top = store.create_ticket({**BASE, "title": "t", "assignee": "m"}, "director")
    kid = store.create_ticket({**BASE, "title": "k", "assignee": "w"}, "m", parent_id=top["id"])
    assert route_for(kid, "blocked") == "manager" and route_for(kid, "technical") == "manager"
    assert route_for(top, "blocked") == "director" and route_for(top, "technical") == "director"
    assert route_for(kid, "safety") == "human" and route_for(kid, "refusal") == "human"
    assert route_for(top, "safety") == "human"


def test_escalate_records_route_and_clears_when_resumed(store):
    top = store.create_ticket({**BASE, "title": "t", "assignee": "m"}, "director")["id"]
    kid = store.create_ticket({**BASE, "title": "k", "assignee": "w"}, "m", parent_id=top)["id"]
    store.transition(kid, "in_progress", "w")
    t = store.escalate(kid, "w", "stuck", kind="blocked")
    assert t["escalation"] == {"to": "manager", "kind": "blocked", "reason": "stuck"}
    assert actions.route_of(t) == "manager"
    store.transition(kid, "in_progress", "mgr")
    assert "escalation" not in store.get_ticket(kid)
    assert actions.route_of(store.get_ticket(kid)) is None


def test_legacy_escalations_without_a_record_are_routed_by_level(store):
    top = store.create_ticket({**BASE, "title": "t", "assignee": "m"}, "director")["id"]
    kid = store.create_ticket({**BASE, "title": "k", "assignee": "w"}, "m", parent_id=top)["id"]
    for tid in (top, kid):
        store.transition(tid, "in_progress", "x")
        store.transition(tid, "escalated", "x", note="old style")          # no escalation record
    assert actions.route_of(store.get_ticket(kid)) == "manager"
    assert actions.route_of(store.get_ticket(top)) == "director"


def test_handoff_puts_it_in_front_of_the_human_with_the_reason(store):
    t = store.create_ticket({**BASE, "title": "t", "assignee": "m"}, "director")["id"]
    store.transition(t, "in_progress", "m")
    with pytest.raises(Exception, match="nothing to hand off"):
        store.handoff(t, "director")
    store.escalate(t, "m", "cannot find the sensor spec", kind="blocked")
    assert actions.route_of(store.get_ticket(t)) == "director"
    actions.handoff(store, t, "need the datasheet from you", "director")
    got = store.get_ticket(t)
    assert actions.route_of(got) == "human" and got["escalation"]["handed_off_by"] == "director"
    assert actions.why_from_events(store.events(t)) == "need the datasheet from you"


# -------------------------------------------------- manager handles workers

def test_manager_retries_a_failed_worker_with_guidance_and_the_fallback_model(store, tmp_path):
    bad = Completion(text="not json", model="flash")
    m, p = mk_manager(store, tmp_path, [
        j(action="delegate", tickets=[WORKER]),
        bad, bad, bad,                                               # worker fails on 'flash' (3 invalid replies)
        j(action="retry_workers", notes="reply format problem", retries=[]),   # filled in below
    ])
    # the manager needs the worker id, which only exists after delegation: script it dynamically
    orig = p.complete
    state = {}

    def smart(**kw):
        n = len(p.calls)
        if n == 4:                                                   # manager's review call
            state["kid"] = store.list_tickets(parent_id=top)[0]["id"]
            p.script[0] = j(action="retry_workers", notes="reply format problem",
                            retries=[{"ticket_id": state["kid"], "guidance": "save long content to a file"}])
            p.script.extend([j(action="done", result={"ok": 1}),     # retried worker succeeds (on 'pro')
                             j(action="done", result={"final": 1})]) # manager finishes
        return orig(**kw)

    p.complete = smart
    top = top_ticket(store)
    t = m.work(top)
    assert t["status"] == "done"
    models = [c["model"] for c in p.calls]
    # manager plans; the worker tries 'flash' then advances to the fallback 'pro' on invalid replies;
    # the manager reviews; the retry starts on the fallback model (not the one that already failed it)
    assert models[:5] == ["m", "flash", "pro", "pro", "m"]
    assert models[5] == "pro"
    review = p.calls[4]["messages"][0]["content"]
    assert "reason: could not obtain a valid reply" in review        # the manager can SEE why it failed
    assert "retries used: 0/2" in review and "began:" in review      # ...and a sample of the bad reply
    assert "save long content to a file" in p.calls[5]["messages"][0]["content"]   # guidance reached the worker
    assert [e for e in store.events(state["kid"]) if e["type"] == "retry"]


def test_retry_limit_is_enforced_and_manager_must_choose_something_else(store, tmp_path):
    m, p = mk_manager(store, tmp_path, [j(action="delegate", tickets=[WORKER])])
    top = top_ticket(store)
    m._delegate(top, {"tickets": [WORKER]})
    kid = store.list_tickets(parent_id=top)[0]["id"]
    store.transition(top, "in_progress", "research_manager")
    store.transition(kid, "in_progress", "w")
    for _ in range(2):                                               # max_worker_retries = 2
        store.escalate(kid, "w", "stuck")
        applied, errors = m._retry_workers(top, {"retries": [{"ticket_id": kid, "guidance": "again"}]})
        assert (applied, errors) == (1, [])
    store.escalate(kid, "w", "stuck again")
    applied, errors = m._retry_workers(top, {"retries": [{"ticket_id": kid, "guidance": "once more"}]})
    assert applied == 0 and "retry limit (2) reached" in errors[0]
    applied, errors = m._retry_workers(top, {"retries": [{"ticket_id": "nope", "guidance": "x"}]})
    assert applied == 0 and "not one of your workers" in errors[0]


def test_worker_problems_never_reach_the_director_or_human_while_the_manager_handles_them(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    cfg["policies"]["max_restarts"] = 0              # no restart budget: the crash must surface at once (see test_daemon.py)
    ctl = Ctl(cfg, tmp_path, MockProvider([
        j(action="delegate", tickets=[WORKER]),
        j(action="escalate", kind="blocked", reason="cannot find the file"),     # worker is stuck
    ]))
    ctl.create(SPEC)
    out = ctl.run()                                                  # script ends at the manager's review -> crash
    # the manager's own call failed (mock exhausted), so the *parent* escalates to the director (technical)
    routes = {a["level"]: a["route"] for a in out["needs_attention"]}
    assert routes == {"top": "director"}
    assert out["managers_handling"] and out["managers_handling"][0]["why"]    # worker's issue stays below
    ctl.close()


def test_safety_and_refusal_go_straight_to_the_human(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    ctl = Ctl(cfg, tmp_path, MockProvider([
        j(action="delegate", tickets=[WORKER]),
        j(action="escalate", kind="safety", reason="heater at 140C and rising"),
        j(action="escalate", kind="blocked", reason="worker reported a safety issue"),
    ]))
    ctl.create(SPEC)
    out = ctl.run()
    worker = [a for a in out["needs_attention"] if a["level"] == "worker"]
    assert worker and worker[0]["route"] == "human" and "140C" in worker[0]["why"]
    ctl.close()

    ctl2 = Ctl(cfg, tmp_path / "second", MockProvider([
        j(action="delegate", tickets=[WORKER]),
        Completion(text="", refused=True, refusal_category="cyber", model="m"),
        Completion(text="", refused=True, refusal_category="cyber", model="m"),
        j(action="escalate", reason="worker was refused"),
    ]))
    ctl2.create(SPEC)
    out2 = ctl2.run()
    refused = [a for a in out2["needs_attention"] if a["level"] == "worker"]
    assert refused and refused[0]["route"] == "human"
    ctl2.close()


def test_manager_settling_its_ticket_closes_out_escalated_workers(store, tmp_path):
    m, p = mk_manager(store, tmp_path, [j(action="delegate", tickets=[WORKER]),
                                        j(action="escalate", kind="blocked", reason="stuck"),
                                        j(action="done", result={"note": "good enough without it"})])
    top = top_ticket(store)
    assert m.work(top)["status"] == "done"
    kid = store.list_tickets(parent_id=top)[0]
    assert kid["status"] == "failed"                                 # no lingering 'escalated' noise
    assert actions.route_of(kid) is None


def test_director_can_handle_or_hand_off(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    ctl = Ctl(cfg, tmp_path, MockProvider([j(action="escalate", kind="blocked", reason="manager is stuck")]))
    tid = ctl.create(SPEC)["ticket_id"]
    out = ctl.run()
    assert [(a["id"], a["route"]) for a in out["needs_attention"]] == [(tid, "director")]
    ctl.handoff(tid, "I cannot fix this; need your decision")
    again = ctl.board()["needs_attention"]
    assert again[0]["route"] == "human" and "need your decision" in again[0]["why"]
    ctl.close()


def test_dashboard_shows_only_human_level_items_plus_a_handling_list(store, tmp_path):
    from fastapi.testclient import TestClient
    from orchestrator.dashboard import create_app
    top = store.create_ticket({**BASE, "title": "Top", "assignee": "m"}, "director")["id"]
    store.transition(top, "in_progress", "m")
    kid = store.create_ticket({**BASE, "title": "Kid", "assignee": "w"}, "m", parent_id=top)["id"]
    store.transition(kid, "in_progress", "w")
    store.escalate(kid, "w", "technical trouble", kind="technical")                  # manager's problem
    unsafe = store.create_ticket({**BASE, "title": "Unsafe", "assignee": "m"}, "director")["id"]
    store.transition(unsafe, "in_progress", "m")
    store.escalate(unsafe, "m", "smoke from the rig", kind="safety")                 # the human's problem
    s = TestClient(create_app(tmp_path / "t.db", load_config(None), project=tmp_path),
                   base_url="http://127.0.0.1").get("/api/state").json()
    assert [n["title"] for n in s["needs_human"]] == ["Unsafe"]
    assert [(n["title"], n["route"]) for n in s["handling"]] == [("Kid", "manager")]
