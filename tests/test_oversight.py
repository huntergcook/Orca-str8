"""Dependencies and critical path, drift review, learnings log, tracing."""
import json

import pytest

from orchestrator import critical_path, learnings
from orchestrator.ctl import Ctl
from orchestrator.providers import MockProvider
from orchestrator.trace import Tracer, to_span

from test_daemon import TICKET, cfg_for, j


def mk(tmp_path, script=None, **pol):
    cfg = cfg_for(tmp_path)
    cfg["policies"].update(pol)
    return Ctl(cfg, cfg["workspace"], MockProvider(script or [])), cfg


def test_depends_on_gates_and_orders(tmp_path):
    ctl, _ = mk(tmp_path)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    b = d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    c = d.create_ticket(dict(TICKET, title="C", depends_on=[b]))
    with pytest.raises(Exception):
        d.create_ticket(dict(TICKET, title="bad", depends_on=["nope"]))
    ana = critical_path.analyze(ctl.store)
    assert [x["id"] for x in ana["critical_chain"]] == [a, b, c]
    assert ana["serial"] and "SERIAL" in ana["message"]
    assert not critical_path.deps_met(ctl.store, ctl.store.get_ticket(b))


def test_independent_work_is_not_serial(tmp_path):
    ctl, _ = mk(tmp_path)
    for t in "XY":
        ctl.director.create_ticket(dict(TICKET, title=t))
    ana = critical_path.analyze(ctl.store)
    assert not ana["serial"] and ana["ready"] == 2
    assert "critical_path" in ctl.board()


def test_failed_dependency_escalates_to_director(tmp_path):
    ctl, _ = mk(tmp_path)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    b = d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    ctl.store.transition(a, "in_progress", "x")
    ctl.store.transition(a, "failed", "x", note="boom")
    assert d._deps_ready(ctl.store.get_ticket(b)) is False
    assert ctl.store.get_ticket(b)["status"] == "escalated"


def test_serial_bottleneck_event(tmp_path):
    ctl, _ = mk(tmp_path, serial_minutes=1, drift_review_minutes=0)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    seen = []
    d.notify = lambda *x: seen.append(x)
    d.oversight()
    d._serial = {k: v - 3600 for k, v in d._serial.items()}
    d._oversight_at = float("-inf")
    d.oversight()
    assert any(e["type"] == "serial_bottleneck" for e in ctl.store.events(None)) and seen


def test_drift_review_reaches_director_prompt(tmp_path):
    ctl, _ = mk(tmp_path, [j(on_track=False, off_charter=["t1"], missing=["crit"], advice="cancel t1")])
    res = ctl.director.drift_review()
    assert not res["on_track"]
    assert "cancel t1" in ctl.director._system_prompt()


def test_charter_change_detected(tmp_path):
    from orchestrator import mission
    ctl, cfg = mk(tmp_path, drift_review_minutes=0)
    ctl.director.oversight()
    mission.set_answer(cfg, "goal", "something else")
    ctl.director._oversight_at = float("-inf")
    ctl.director.oversight()
    assert any(e["type"] == "charter_changed" for e in ctl.store.events(None))


def test_learnings_roundtrip(tmp_path):
    n = learnings.record(tmp_path, "t1", "Try fix", {"dead_ends": ["approach A fails"], "learned": "B works",
                                                      "outcome": "refuted", "summary": "no"}, {"hypothesis": "A helps"})
    assert n == 3
    s = learnings.section(tmp_path)
    assert "DEAD END" in s and "EXPERIMENT REFUTED" in s


def test_trace_file_and_span(tmp_path):
    ctl, cfg = mk(tmp_path)
    tr = Tracer({"trace": {"file": True}}, tmp_path)
    ctl.store.tracer = tr
    ctl.director.create_ticket(dict(TICKET))
    lines = (tmp_path / "traces.jsonl").read_text().splitlines()
    span = json.loads(lines[0])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    assert to_span({"seq": 1, "ts": "2026-01-01T00:00:00+00:00", "actor": "a", "type": "t", "payload": {}}, None)["name"] == "a:t"


def test_parallel_cap_backs_off():
    from orchestrator import resources
    cfg = {"policies": {"max_parallel_tickets": 8, "min_free_memory_mb": 1000, "max_cpu_load": 1.5}}
    assert resources.parallel_cap(cfg, mem_mb=8000, load=0.2)[0] == 8
    assert resources.parallel_cap(cfg, mem_mb=800, load=0.2)[0] == 4
    assert resources.parallel_cap(cfg, mem_mb=100, load=0.2)[0] == 1
    cap, why = resources.parallel_cap(cfg, mem_mb=8000, load=3.0)
    assert cap == 4 and "cpu" in why


def test_routing_demotes_poor_model_only_with_evidence():
    from orchestrator import routing
    card = {"a": {"done": 1, "bad": 5}, "b": {"done": 5, "bad": 0}}
    assert routing.reorder(["a", "b"], card) == ["b", "a"]
    assert routing.reorder(["a", "b"], {"a": {"done": 0, "bad": 2}, "b": {"done": 5, "bad": 0}}) == ["a", "b"]  # too few samples
    assert routing.reorder(["a", "b"], None) == ["a", "b"]


def test_scorecard_from_a_store(tmp_path):
    ctl, _ = mk(tmp_path)
    assert ctl.board()["model_scorecard"] == {}


def test_director_decide_action_is_logged_and_shown(tmp_path):
    ctl, _ = mk(tmp_path)
    ctl.director._act({"type": "decide", "decision": "drop feature Y", "basis": "not_goals", "reversible": True})
    assert "drop feature Y" in (ctl.director.workspace / "DECISIONS.md").read_text()
    assert "drop feature Y" in ctl.director._system_prompt()
    assert any(e["type"] == "decision" for e in ctl.store.events(None))


def test_report_and_evidence(tmp_path):
    ctl, cfg = mk(tmp_path)
    ctl.director._act({"type": "decide", "decision": "ship small", "basis": "rigor"})
    ctl.director.create_ticket(dict(TICKET, title="Survey X"))
    out = ctl.report()
    assert "ship small" in out["text"] and "Survey X" in out["text"] and "X works" in out["text"]
    assert (ctl.workspace / "REPORT.md").is_file()


def test_daemon_saves_check_evidence(tmp_path):
    from test_daemon import daemon
    d, cfg, _ = daemon(tmp_path, [j(say="ok", actions=[TICKET]), j(action="done", notes="n", result={"summary": "s"})])
    d._checks(Ctl(cfg, cfg["workspace"], MockProvider([])))
    assert list((d.workspace / "evidence").glob("checks-*.txt"))


def test_bench_runs_a_goal_offline_and_compares(tmp_path):
    import sys
    from orchestrator import bench
    ok = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    spec = {"name": "g1", "goal": "do it", "success_checks": [ok], "files": {"a.txt": "x"}}
    script = [j(say="go", actions=[TICKET]), j(action="done", notes="n", result={"summary": "s"}),
              j(say="done", actions=[])] + [j(say="x", actions=[])] * 6
    cfg_patch = {"restart_backoff_seconds": 0}
    from orchestrator.providers import MockProvider
    res = bench.run_goal(spec, tmp_path, provider=MockProvider(script), max_minutes=0.1, cfg_patch=cfg_patch)
    assert res["goal"] == "g1" and "finished" in res
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps({**res, "finished": True, "tokens": 10}) + "\n" + json.dumps({**res, "finished": False, "tokens": 10}) + "\n")
    assert "REGRESSION" in bench.compare(p)
