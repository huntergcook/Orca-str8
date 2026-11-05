import json

import pytest

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.manager import Manager
from orchestrator.providers import Completion, MockProvider
from orchestrator.director import Director
from orchestrator.verification import Verifier


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def j(**kw):
    return json.dumps(kw)


SPEC = dict(title="Survey", worker_role="surveyor", context="find papers",
            acceptance_criteria=["3 papers"], return_spec={"format": "summary", "description": "d"})


def passing():
    return j(verdict="pass", issues=[], advice="")


def failing(msg="only 1 paper cited"):
    return j(verdict="fail", issues=[msg], advice="find 2 more")


def setup(store, tmp_path, script, verifier_models=("va", "vb"), rigor=None):
    cfg = load_config(None)
    cfg["verification"]["enabled"] = True
    p = MockProvider(script)
    v = Verifier(store, p, cfg, tmp_path, list(verifier_models))
    m = Manager("research_manager", store, p, cfg, tmp_path, "You manage research.", ["m"], ["w"], verifier=v)
    spec = dict(SPEC, assignee="research_manager", worker_role="worker")
    if rigor:
        spec["rigor"] = rigor
    tid = store.create_ticket(spec, "director")["id"]
    return m, p, tid


def test_pass_by_all_verifiers_then_done(store, tmp_path):
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={"s": 1}),          # manager finishes directly
        passing(), passing(),                        # two verifiers
    ])
    t = m.work(tid)
    assert t["status"] == "done"
    assert [c["model"] for c in p.calls[1:]] == ["va", "vb"]                 # diverse models
    assert len(t["verification_notes"]) == 2
    assert any(e["type"] == "verification" for e in store.events(tid))


def test_worst_verdict_wins_and_manager_fixes_then_passes(store, tmp_path):
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={"papers": 1}),
        passing(), failing(),                                        # round 1: one verifier fails
        j(action="delegate", tickets=[SPEC]),                        # manager acts on the advice
        j(action="done", result={"papers": 3}),                      # worker
        j(action="done", result={"papers": 3}),                      # manager re-reviews
        passing(), passing(),                                        # round 2 passes
    ])
    t = m.work(tid)
    assert t["status"] == "done" and t["result"] == {"papers": 3}
    verdicts = [e["payload"]["verdict"] for e in store.events(tid) if e["type"] == "verification"]
    assert verdicts == ["fail", "pass"]
    manager_followup = p.calls[3]["messages"][0]["content"]          # manager's call right after the report
    assert "only 1 paper" in manager_followup and "Verification report" in manager_followup
    assert not any(e["type"] == "verification_overridden" for e in store.events(tid))


def test_manager_override_is_logged_and_respected(store, tmp_path):
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={"x": 1}),
        failing("missing error bars"), passing(),
        j(action="done", result={"x": 1}, override_reason="quick_and_dirty goal, error bars not needed"),
    ])
    t = m.work(tid)
    assert t["status"] == "done"
    ov = [e for e in store.events(tid) if e["type"] == "verification_overridden"]
    assert ov and "error bars not needed" in ov[0]["payload"]["reason"]
    assert len(p.calls) == 4                                          # no re-verification after override


def test_round_cap_lets_manager_decision_stand(store, tmp_path):
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={"v": 1}),
        failing(), failing(),                                         # round 1
        j(action="done", result={"v": 2}),                            # manager retries without override_reason
        failing(), failing(),                                         # round 2 (max_rounds=2)
        j(action="done", result={"v": 3}),                            # round cap hit: this decision stands
    ])
    m.cfg["verification"]["max_rounds"] = 2
    t = m.work(tid)
    assert t["status"] == "done"
    assert len([e for e in store.events(tid) if e["type"] == "verification"]) == 2
    assert any(e["type"] == "verification_overridden" for e in store.events(tid))


def test_verifier_sees_scope_evidence_and_rigor(store, tmp_path):
    (tmp_path / "memory" / "director").mkdir(parents=True)
    (tmp_path / "memory" / "director" / "state.md").write_text("GOAL: characterize the sensor drift")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "out.csv").write_text("t,v\n1,2")
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={"data": "data/out.csv", "note": "../../etc/passwd"}),
        passing(),
    ], verifier_models=("va",), rigor="rigorous")
    m.work(tid)
    vcall = p.calls[1]
    user = vcall["messages"][0]["content"]
    assert "characterize the sensor drift" in user                   # broader goal
    assert "t,v" in user                                              # evidence file contents
    assert "etc/passwd" in user and "root:" not in user              # escaping path ignored, not read
    assert "RIGOROUS" in vcall["system"]


def test_verifier_refusal_and_garbage_do_not_crash(store, tmp_path):
    m, p, tid = setup(store, tmp_path, [
        j(action="done", result={}),
        Completion(text="", refused=True, refusal_category="cyber", model="va"),
        "nonsense", "still nonsense",
        j(action="done", result={}, override_reason="verifiers unavailable"),
    ])
    t = m.work(tid)
    assert t["status"] == "done"
    notes = " ".join(store.get_ticket(tid)["verification_notes"])
    assert "declined" in notes and "no valid verdict" in notes


def test_disabled_by_default_and_skipped_for_verification_dept(store, tmp_path):
    cfg = load_config(None)
    assert cfg["verification"]["enabled"] is False
    cfg["verification"]["enabled"] = True
    cfg["verification"]["verifier_models"] = ["va", "vb"]
    cfg["departments"] = {"research": {}, "verification": {"diverse_models": True}}
    top = Director(store, MockProvider([]), cfg, tmp_path)
    assert top.managers["research"].verifier is not None
    assert top.managers["verification"].verifier is None             # verifiers aren't verified
    assert top.managers["verification"].diverse_workers
    assert top.managers["verification"].worker_models == ["va", "vb"]


def test_diverse_worker_rotation(store, tmp_path):
    cfg = load_config(None)
    p = MockProvider([j(action="delegate", tickets=[SPEC, SPEC, SPEC]),
                      j(action="done", result={}), j(action="done", result={}), j(action="done", result={}),
                      j(action="done", result={})])
    m = Manager("verification_manager", store, p, cfg, tmp_path, "role", ["m"], ["va", "vb"], diverse_workers=True)
    tid = store.create_ticket(dict(SPEC, assignee="verification_manager", worker_role="worker"), "director")["id"]
    m.work(tid)
    # workers run in parallel, so call order varies; the rotation still spreads them across both models
    assert sorted(c["model"] for c in p.calls[1:4]) == ["va", "va", "vb"]
