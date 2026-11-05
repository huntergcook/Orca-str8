import sqlite3

import pytest

from orchestrator import InvalidTransition, NotFound, ResourceBusy, Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def base(**over):
    t = dict(
        title="Survey X",
        assignee="research_manager",
        worker_role="literature_surveyor",
        context="Find prior work on X. See workspace/notes/x.md",
        acceptance_criteria=["5+ relevant papers summarized"],
        return_spec={"format": "summary", "description": "Markdown summary"},
    )
    t.update(over)
    return t


def test_create_and_get(store):
    t = store.create_ticket(base(), actor="director")
    got = store.get_ticket(t["id"])
    assert got["status"] == "open"
    assert got["path_trace"] == ["director"]
    assert got["created_by"] == "director"


def test_validation_rejects_bad_ticket(store):
    bad = base()
    del bad["return_spec"]
    with pytest.raises(StoreError, match="return_spec"):
        store.create_ticket(bad, actor="director")
    with pytest.raises(StoreError):
        store.create_ticket(base(rigor="sloppy"), actor="director")
    assert store.list_tickets() == []


def test_child_extends_path_trace_and_parent(store):
    p = store.create_ticket(base(), actor="director")
    c = store.create_ticket(base(assignee="worker-1"), actor="research_manager", parent_id=p["id"])
    assert c["path_trace"] == ["director", p["id"]]
    assert store.get_ticket(p["id"])["children"] == [c["id"]]


def test_fanout_cap(store):
    p = store.create_ticket(base(), actor="director")
    store.create_ticket(base(), actor="m", parent_id=p["id"], max_children=1)
    with pytest.raises(StoreError, match="children"):
        store.create_ticket(base(), actor="m", parent_id=p["id"], max_children=1)


def test_transitions(store):
    t = store.create_ticket(base(), actor="director")
    store.transition(t["id"], "in_progress", "worker-1")
    done = store.transition(t["id"], "done", "worker-1", result={"summary": "ok"})
    assert done["result"] == {"summary": "ok"}
    with pytest.raises(InvalidTransition):
        store.transition(t["id"], "in_progress", "worker-1")
    with pytest.raises(InvalidTransition):
        store.transition(store.create_ticket(base(), actor="x")["id"], "done", "w")


def test_clarification_flow(store):
    t = store.create_ticket(base(), actor="director")
    store.transition(t["id"], "in_progress", "w")
    q = store.ask_question(t["id"], "w", "Which year range?")
    assert q["status"] == "needs_clarification"
    a = store.answer_question(t["id"], "research_manager", "2020+")
    assert a["status"] == "in_progress"
    assert a["questions"][0]["answer"] == "2020+"
    with pytest.raises(StoreError):
        store.answer_question(t["id"], "m", "again")


def test_events_logged_and_append_only(store):
    t = store.create_ticket(base(), actor="director")
    store.transition(t["id"], "in_progress", "w", note="start")
    evs = store.events(t["id"])
    assert [e["type"] for e in evs] == ["created", "status_changed"]
    assert evs[1]["payload"]["to"] == "in_progress"
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE events SET actor='x'")


def test_failed_transition_leaves_no_event(store):
    t = store.create_ticket(base(), actor="director")
    with pytest.raises(InvalidTransition):
        store.transition(t["id"], "done", "w")
    assert [e["type"] for e in store.events(t["id"])] == ["created"]


def test_list_filters(store):
    a = store.create_ticket(base(assignee="a"), actor="t")
    store.create_ticket(base(assignee="b"), actor="t")
    store.transition(a["id"], "in_progress", "a")
    assert len(store.list_tickets(assignee="a")) == 1
    assert len(store.list_tickets(status="open")) == 1


def test_not_found(store):
    with pytest.raises(NotFound):
        store.get_ticket("nope")


def test_resource_claim_is_exclusive(store):
    store.register_resource("scope-1", safe_to_stop="yes between sweeps")
    store.claim_resource("scope-1", "T1", "2h", actor="w1")
    with pytest.raises(ResourceBusy):
        store.claim_resource("scope-1", "T2")
    with pytest.raises(StoreError):
        store.release_resource("scope-1", "T2")
    store.heartbeat_resource("scope-1", "T1")
    store.release_resource("scope-1", "T1")
    store.claim_resource("scope-1", "T2")
    assert store.list_resources()[0]["holder_ticket"] == "T2"
    with pytest.raises(NotFound):
        store.claim_resource("missing", "T1")


def test_safe_to_stop(store):
    store.register_resource("r")
    store.set_safe_to_stop("r", "stop after current sweep point")
    assert store.list_resources()[0]["safe_to_stop"] == "stop after current sweep point"
