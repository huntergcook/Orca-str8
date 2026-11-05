import json

import pytest

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.manager import Manager
from orchestrator.providers import Completion, MockProvider
from orchestrator.director import Director


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def j(**kw):
    return json.dumps(kw)


SPEC = dict(title="Survey", worker_role="surveyor", context="find papers",
            acceptance_criteria=["3 papers"], return_spec={"format": "summary", "description": "d"})


def mk_manager(store, tmp_path, script):
    cfg = load_config(None)
    p = MockProvider(script)
    return Manager("research_manager", store, p, cfg, tmp_path, "You manage research.", ["m"], ["w"]), p


def top_ticket(store):
    return store.create_ticket(dict(SPEC, assignee="research_manager", worker_role="worker"), "director")["id"]


def test_plan_run_review(store, tmp_path):
    m, p = mk_manager(store, tmp_path, [
        j(action="delegate", notes="split it", tickets=[SPEC]),
        j(action="done", result={"summary": "worker found it"}),         # worker
        j(action="done", notes="all good", result={"final": 1}),         # review
    ])
    tid = top_ticket(store)
    t = m.work(tid)
    assert t["status"] == "done" and t["result"] == {"final": 1}
    kids = store.list_tickets(parent_id=tid)
    assert len(kids) == 1 and kids[0]["status"] == "done"
    assert kids[0]["path_trace"] == ["director", tid]
    assert "split it" in m.memory.read_journal()
    assert [c["model"] for c in p.calls] == ["m", "w", "m"]


def test_clarification_answered_by_manager(store, tmp_path):
    m, p = mk_manager(store, tmp_path, [
        j(action="delegate", tickets=[SPEC]),
        j(action="ask", question="which years?"),                         # worker asks
        j(action="answer", answer="2020+"),                               # manager answers
        j(action="done", result={}),                                      # worker resumes
        j(action="done", result={"ok": True}),                            # review
    ])
    t = m.work(top_ticket(store))
    assert t["status"] == "done"


def test_manager_asks_upward_when_it_cannot_answer(store, tmp_path):
    m, p = mk_manager(store, tmp_path, [
        j(action="delegate", tickets=[SPEC]),
        j(action="ask", question="budget?"),
        j(action="ask", question="human, what is the budget?"),           # manager asks upward
    ])
    t = m.work(top_ticket(store))
    assert t["status"] == "needs_clarification"


def test_fanout_cap_respected(store, tmp_path):
    # asking for 6 workers with a cap of 5 creates 5; the manager is told, not crashed
    m, p = mk_manager(store, tmp_path, [j(action="delegate", tickets=[SPEC] * 6)]
                      + [j(action="done", result={"w": 1})] * 5
                      + [j(action="done", result={"final": "five were enough"})])
    m.cfg["policies"]["max_children_per_agent"] = 5
    top = top_ticket(store)
    t = m.work(top)
    assert t["status"] == "done"
    assert len(store.list_tickets(parent_id=top)) == 5                    # the cap held
    assert "already has 5 children" in p.calls[-1]["messages"][0]["content"]   # and the manager was told why


def test_garbage_escalates(store, tmp_path):
    m, _ = mk_manager(store, tmp_path, ["junk", "junk", "junk"])
    assert m.work(top_ticket(store))["status"] == "escalated"


def test_director_end_to_end(store, tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {"mission": "do research"}}
    top_reply = j(say="On it. What rigor do you want?", actions=[
        {"type": "set_state", "text": "goal: survey X"},
        {"type": "create_ticket", "department": "research", **{k: SPEC[k] for k in
            ("title", "context", "acceptance_criteria", "return_spec")}}])
    p = MockProvider([top_reply,
                      j(action="delegate", tickets=[SPEC]),
                      j(action="done", result={"s": 1}),
                      j(action="done", result={"final": 1})])
    top = Director(store, p, cfg, tmp_path)
    say = top.turn("Survey X please")
    assert say.startswith("On it")
    board = top.board()
    assert "[done]" in board and "research_manager" in board
    assert "goal: survey X" in top.memory.read_state()
    log = top.log_path.read_text()
    assert "Survey X please" in log and "On it" in log
    # next turn's system prompt is rebuilt from files, not chat history
    p.script.append(j(say="Done?", actions=[{"type": "finish", "summary": "met"}]))
    top.turn("looks good")
    assert top.finished
    assert "goal: survey X" in p.calls[-1]["system"] and "[done]" in p.calls[-1]["system"]


def test_director_refusal_is_surfaced_not_rerouted(store, tmp_path):
    cfg = load_config(None)
    p = MockProvider([Completion(text="", refused=True, refusal_category="cyber", model="m")])
    top = Director(store, p, cfg, tmp_path)
    assert "declined" in top.turn("do something")
    assert len(p.calls) == 1
