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


def test_bench_project_is_its_own_git_repo(tmp_path):
    import subprocess
    from orchestrator import bench
    (tmp_path / "a.py").write_text("x = 1\n")
    bench._git_init(tmp_path)
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "bench start" in git("log", "--oneline") and "a.py" in git("ls-files")


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


# ----------------------------------------------------------------- one config file, easy setup, fan-out

def test_orca_yaml_is_the_single_config_and_set_answer_keeps_comments(tmp_path):
    from orchestrator import init, mission
    from orchestrator.config import load_config, model_chain
    init.scaffold(tmp_path, cli="codex")
    f = tmp_path / "orca.yaml"
    cfg = load_config(None, project=tmp_path)
    assert model_chain(cfg, "worker") == ["codex-cli:default"] and set(cfg["departments"]) >= {"research", "software"}
    assert not mission.status(cfg)["complete"]
    mission.set_answer(cfg, "goal", "build X")
    mission.set_answer(cfg, "success_criteria", ["a", "b"])
    mission.set_answer(cfg, "success_checks", ["pytest -q"])
    text = f.read_text()
    assert "# REQUIRED" in text and "models:" in text                      # comments and other sections survive
    cfg = load_config(None, project=tmp_path)
    st = mission.status(cfg)
    assert st["complete"] and mission.success_checks(cfg) == ["pytest -q"]
    charter = mission.render_charter(cfg)
    assert "build X" in charter and "balanced" in charter and "director: strongest model" in charter


def test_orca_yaml_overrides_and_limits(tmp_path):
    from orchestrator.config import load_config, model_chain
    (tmp_path / "orca.yaml").write_text("mission: {goal: g, done_when: [d]}\nmodels:\n  cli: claude\n  worker: haiku\n  director: opus\n"
                                        "limits: {parallel_workers: 20, max_hours: 2}\nverify: false\n")
    cfg = load_config(None, project=tmp_path)
    assert model_chain(cfg, "worker") == ["claude-cli:haiku"] and model_chain(cfg, "manager") == ["claude-cli:default"]
    assert cfg["policies"]["max_parallel_workers"] == 20 and cfg["budget"]["max_minutes"] == 120
    assert cfg["verification"]["enabled"] is False


def test_init_one_command_sets_up_and_records_the_mission(tmp_path):
    from orchestrator import init, mission
    from orchestrator.config import load_config
    root = tmp_path / "p"
    assert init.main([str(root), "--cli", "claude", "--goal", "make hello", "--done-when", "prints hello",
                      "--check", "python hello.py", "--no-git"]) == 0
    cfg = load_config(None, project=root)
    assert mission.status(cfg)["complete"] and mission.success_checks(cfg) == ["python hello.py"]
    assert (root / "AGENTS.md").exists() and not (root / "config").exists()
    assert init.main([str(root), "--cli", "claude", "--no-git"]) == 0           # safe to re-run: nothing is overwritten
    assert "make hello" in (root / "orca.yaml").read_text()


def test_orca_command_dispatch(tmp_path, capsys):
    from orchestrator import orca
    assert orca.main(["help"]) == 0 and "orca start" in capsys.readouterr().out


def test_director_can_put_a_known_model_on_a_ticket_and_rejects_unknown(tmp_path):
    ctl, _ = mk(tmp_path)
    d = ctl.director
    known = sorted(d._known_models())[0]
    tid = d.create_ticket(dict(TICKET, models={"worker": known}))
    assert ctl.store.get_ticket(tid)["models"] == {"worker": known}
    with pytest.raises(ValueError):
        d.create_ticket(dict(TICKET, models={"worker": "no-such-model"}))
    assert known in d._system_prompt()


def test_capacity_nudge_asks_the_director_to_fan_out(tmp_path):
    ctl, _ = mk(tmp_path, [j(say="ok", actions=[])], capacity_nudge_minutes=1)
    d = ctl.director
    d.create_ticket(dict(TICKET, title="only one"))
    ana = critical_path.analyze(ctl.store)
    d._capacity_nudge(ana, 1000.0)                    # starts the clock
    d._capacity_nudge(ana, 1000.0 + 61)               # a quiet period has passed
    assert any(e["type"] == "capacity_nudge" for e in ctl.store.events(None))
    assert any("Look for independent work" in m["messages"][0]["content"] for m in ctl.provider.inner.calls)
    d._capacity_nudge(ana, 1000.0 + 62)               # but only once per quiet period
    assert len(ctl.provider.inner.calls) == 1


def test_manager_planning_prompt_pushes_fan_out(tmp_path):
    from orchestrator.config import load_config
    assert load_config(None)["policies"]["max_parallel_workers"] >= 8
    assert "fan out" in open("roles/director.md").read().lower()
