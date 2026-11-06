"""Mission brief, persistence (daemon, restarts, halts) and the director rename."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from orchestrator import mission, providers, winsys
from orchestrator.config import load_config, model_chain, project_rules
from orchestrator.ctl import Ctl
from orchestrator.daemon import Daemon, run_checks
from orchestrator.notify import Notifier
from orchestrator.providers import Completion, MockProvider
from orchestrator.store import StoreError


def j(**kw):
    return json.dumps(kw)


WORKER = dict(title="W", worker_role="surveyor", context="c", acceptance_criteria=["a"],
              return_spec={"format": "f", "description": "d"})
TICKET = dict(type="create_ticket", department="research", title="Survey X", context="find things",
              acceptance_criteria=["3 papers"], return_spec={"format": "summary", "description": "d"})
PASS_CMD = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, level, title, body="", key=None, min_gap=0.0):
        self.calls.append((level, title, body))
        return True


def cfg_for(tmp_path, answered=True, checks=None):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    cfg["workspace"] = str(tmp_path / "ws")
    cfg["mission_file"] = str(tmp_path / "mission.yaml")
    cfg["policies"]["restart_backoff_seconds"] = 0
    cfg["policies"]["daemon_poll_seconds"] = 0
    mission.scaffold(cfg["mission_file"])
    if answered:
        for qid, ans in (("goal", "do X"), ("success_criteria", ["X works"]), ("rigor", "balanced"),
                         ("autonomy", "assume_and_continue"), ("model_access", "claude cli")):
            mission.set_answer(cfg, qid, ans)
    if checks is not None:
        mission.set_answer(cfg, "success_checks", checks)
    return cfg


def daemon(tmp_path, script, cfg=None, **kw):
    cfg = cfg or cfg_for(tmp_path, checks=[PASS_CMD])
    note = Capture()
    return Daemon(tmp_path, cfg=cfg, provider=MockProvider(script), sleep=lambda s: None, notifier=note, **kw), cfg, note


# ------------------------------------------------------------------ mission brief

def test_blank_brief_blocks_until_required_questions_are_answered(tmp_path):
    cfg = cfg_for(tmp_path, answered=False)
    st = mission.status(cfg)
    assert not st["complete"] and {q["id"] for q in st["unanswered_required"]} == {"goal", "success_criteria"}
    assert "incomplete" in mission.gate_error(cfg)
    for qid in ("goal", "success_criteria"):
        mission.set_answer(cfg, qid, "x")
    assert mission.status(cfg)["complete"] and mission.gate_error(cfg) is None


def test_charter_reaches_every_prompt_and_unknown_question_is_rejected(tmp_path):
    cfg = cfg_for(tmp_path, checks=["pytest -q"])
    rules = project_rules(cfg)
    assert "do X" in rules and "X works" in rules and "binding" in rules
    assert mission.success_checks(cfg) == ["pytest -q"]
    with pytest.raises(KeyError):
        mission.set_answer(cfg, "nonsense", "x")
    assert mission.charter_hash(cfg) == mission.charter_hash(cfg)


def test_project_without_a_brief_is_not_gated(tmp_path):
    cfg = load_config(None)
    cfg["mission_file"] = str(tmp_path / "none.yaml")
    assert mission.gate_error(cfg) is None and project_rules(cfg) == ""


def test_ctl_create_is_gated_and_intake_records_answers(tmp_path):
    cfg = cfg_for(tmp_path, answered=False)
    ctl = Ctl(cfg, cfg["workspace"], MockProvider([]))
    with pytest.raises(StoreError, match="mission brief"):
        ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                        return_spec={"format": "f", "description": "d"}))
    st = ctl.intake([("goal", "ship it"), ("success_checks", '["a", "b"]')])
    assert "goal" in st["answered"] and mission.success_checks(cfg) == ["a", "b"]
    with pytest.raises(StoreError, match="no question"):
        ctl.intake([("bogus", "x")])
    ctl.close()


# ------------------------------------------------------------------ the rename

def test_legacy_names_are_migrated(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory" / "top_manager").mkdir(parents=True)
    (ws / "memory" / "top_manager" / "state.md").write_text("old notes")
    cfg = cfg_for(tmp_path)
    ctl = Ctl(cfg, ws, MockProvider([]))
    assert ctl.director.memory.read_state() == "old notes"          # memory folder moved
    tid = ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                          return_spec={"format": "f", "description": "d"}))["ticket_id"]
    ctl.close()
    db = sqlite3.connect(ws / "orchestrator.db")
    db.execute("UPDATE tickets SET created_by='top_manager', data=REPLACE(data,'director','top_manager')")
    db.commit()
    db.close()
    from orchestrator.store import Store
    s = Store(ws / "orchestrator.db")                                 # reopening migrates the live rows
    t = s.get_ticket(tid)
    assert t["created_by"] == "director" and "director" in t["cc"] and "top_manager" not in json.dumps(t)
    s.close()


def test_old_top_tier_in_a_config_file_still_works(tmp_path):
    p = tmp_path / "o.yaml"
    p.write_text("models:\n  top: {provider: external, model: claude-code}\n")
    cfg = load_config(p)
    assert cfg["models"]["director"]["provider"] == "external" and "top" not in cfg["models"]


# ------------------------------------------------------------------ the daemon

def test_daemon_waits_for_the_mission_and_scaffolds_it(tmp_path):
    cfg = cfg_for(tmp_path, answered=False)
    (tmp_path / "mission.yaml").unlink()
    d, _, note = daemon(tmp_path, [], cfg=cfg)
    assert d.tick() == "waiting_mission"
    assert (tmp_path / "mission.yaml").exists() and any("mission" in c[1].lower() for c in note.calls)


def test_daemon_runs_a_goal_to_completion_without_anyone_watching(tmp_path):
    script = [
        j(say="starting", actions=[TICKET]),                       # director, event "start"
        j(action="delegate", tickets=[WORKER]),                    # research manager plans
        j(action="done", result={"s": 1}),                         # worker
        j(action="done", result={"final": 1}),                     # manager concludes
        j(say="results read, goal looks met", actions=[]),         # director, event "completed"
    ]
    d, cfg, note = daemon(tmp_path, script)
    phases = [d.tick() for _ in range(3)]
    assert phases == ["working", "working", "finished"]
    assert (d.workspace / "FINISHED").exists()
    assert note.calls[-1][0] == "done"
    assert d.run(max_ticks=1) == "finished"                        # a finished project never restarts work


def test_failing_checks_keep_the_daemon_working_instead_of_stopping(tmp_path):
    cfg = cfg_for(tmp_path, checks=[FAIL_CMD])
    script = [j(say="go", actions=[TICKET]), j(action="delegate", tickets=[WORKER]), j(action="done", result={}),
              j(action="done", result={}), j(say="ok", actions=[]),
              j(say="gap analysis", actions=[{**TICKET, "title": "Close the gap"}])]
    d, _, note = daemon(tmp_path, script, cfg=cfg)
    for _ in range(3):
        d.tick()
    assert not (d.workspace / "FINISHED").exists()
    ctl = Ctl(cfg, d.workspace, MockProvider([]))
    assert any(t["title"] == "Close the gap" for t in ctl.store.list_tickets())     # it asked the director for the next step
    ctl.close()


def test_the_director_cannot_end_the_project_before_the_checks_pass(tmp_path):
    cfg = cfg_for(tmp_path, checks=[FAIL_CMD])
    d, _, _ = daemon(tmp_path, [j(say="done!", actions=[{"type": "finish", "summary": "all good"}])], cfg=cfg)
    ctl = Ctl(cfg, d.workspace, d.provider)
    ctl.director.finish_gate = lambda: d.finish_gate(ctl)
    ctl.director.event_turn("test", "finish now")
    assert not ctl.director.finished and "failing" in ctl.director.finish_rejection
    assert any(e["type"] == "finish_refused" for e in ctl.store.events())
    ctl.close()


def test_pause_quota_and_stop_are_waited_out_not_failed(tmp_path):
    d, cfg, note = daemon(tmp_path, [])
    (d.workspace / "PAUSE").write_text("x")
    assert d.tick() == "paused"
    (d.workspace / "PAUSE").unlink()
    model = model_chain(cfg, "manager")[0]
    providers.mark_unavailable(model, "usage limit")
    try:
        assert d.tick() == "waiting_quota" and any(c[0] == "blocked" for c in note.calls)
        cfg["policies"]["model_retry_minutes"] = 0                  # the wait is over: the model is tried again
        assert d.tick() != "waiting_quota" and not providers.UNAVAILABLE
    finally:
        providers.UNAVAILABLE.clear()
    (d.workspace / "STOP").write_text("x")
    assert d.run(max_ticks=5) == "stopped"


def test_a_crashing_cycle_is_logged_and_retried_not_fatal(tmp_path):
    d, _, note = daemon(tmp_path, [])
    n = {"i": 0}

    def boom():
        n["i"] += 1
        if n["i"] < 3:
            raise RuntimeError("disk hiccup")
        return "stopped"
    d.tick = boom
    assert d.run(max_ticks=5) == "stopped" and n["i"] == 3
    assert "disk hiccup" in d.log_path.read_text()


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows-only")
def test_job_object_and_keep_awake_really_work_on_windows():
    r = subprocess.run([sys.executable, "-c", "from orchestrator import winsys; "
                        "print(winsys.kill_children_on_exit(), winsys.keep_awake())"], capture_output=True, text=True)
    assert r.stdout.split() == ["True", "True"], r.stdout + r.stderr


def test_restart_clears_the_stuck_counter(tmp_path):
    d, cfg, _ = daemon(tmp_path, [])
    d._save(gap_turns=3, last_gap_sig="x")
    d.run(max_ticks=0)                       # acquires the lock and records the (re)start, runs no cycle
    assert d.state["gap_turns"] == 0


def test_a_second_daemon_for_the_same_project_is_refused(tmp_path):
    a, cfg, _ = daemon(tmp_path, [])
    from orchestrator.util import atomic_write, process_stamp
    atomic_write(a.workspace / "DAEMON_RUN", process_stamp(os.getppid()))   # our parent stands in for another live daemon (pid 1 doesn't exist on Windows)
    assert a.run(max_ticks=1) == "already_running"


# ------------------------------------------------------------------ restarts instead of escalation

class Flaky:
    """Raises on the first `n` calls, then answers from a script."""
    def __init__(self, n, script):
        self.n, self.inner = n, MockProvider(script)

    def complete(self, **kw):
        if self.n > 0:
            self.n -= 1
            raise RuntimeError("provider outage")
        return self.inner.complete(**kw)


def test_a_manager_that_crashes_is_restarted_and_finishes(tmp_path):
    cfg = cfg_for(tmp_path)
    ctl = Ctl(cfg, cfg["workspace"], Flaky(2, [j(action="delegate", tickets=[WORKER]), j(action="done", result={}),
                                                j(action="done", result={"ok": 1})]))
    tid = ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                          return_spec={"format": "f", "description": "d"}))["ticket_id"]
    ctl.run()
    t = ctl.store.get_ticket(tid)
    assert t["status"] == "done"
    assert sum(e["type"] == "restarted" for e in ctl.store.events(tid)) >= 1
    ctl.close()


def test_restarts_are_limited_then_the_problem_climbs_the_chain(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["policies"]["max_restarts"] = 2
    ctl = Ctl(cfg, cfg["workspace"], Flaky(10**6, []))
    tid = ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                          return_spec={"format": "f", "description": "d"}))["ticket_id"]
    ctl.run()
    ev = ctl.store.events(tid)
    assert ctl.store.get_ticket(tid)["status"] == "escalated"
    assert sum(e["type"] == "restarted" for e in ev) == 2
    ctl.close()


# ------------------------------------------------------------------ notifications, checks, Windows helpers

def test_notifier_logs_always_and_survives_a_dead_channel(tmp_path):
    sent = []

    def post(url, body, headers):
        sent.append(url)
        if "bad" in url:
            raise OSError("down")
    n = Notifier({"notify": {"ntfy": "my-topic", "webhook": "https://bad.example/hook", "command": None}}, tmp_path, post=post)
    assert n("blocked", "Title", "Body") is True
    assert sent == ["https://ntfy.sh/my-topic", "https://bad.example/hook"]
    assert "Title" in (tmp_path / "notifications.log").read_text()
    assert n("info", "x", key="k", min_gap=3600) is True and n("info", "x", key="k", min_gap=3600) is False


def test_run_checks_reports_pass_fail_and_timeouts(tmp_path):
    res = run_checks([PASS_CMD, FAIL_CMD, f'"{sys.executable}" -c "import time; time.sleep(5)"'], tmp_path, timeout=1)
    assert [r["ok"] for r in res] == [True, False, False] and "timed out" in res[2]["tail"]


def test_windows_helpers_are_safe_everywhere_and_build_the_right_commands(tmp_path):
    import os
    if os.name != "nt":
        assert winsys.keep_awake() is False and winsys.kill_children_on_exit() is False
    args = winsys.task_scheduler_args(tmp_path)
    assert args[:2] == ["schtasks", "/Create"] and "ONLOGON" in args and "orchestrator.daemon" in args[-1]
    assert "Restart=always" in winsys.systemd_unit(tmp_path)


def test_status_reports_without_a_running_daemon(tmp_path):
    d, _, _ = daemon(tmp_path, [])
    st = d.status()
    assert st["phase"] == "not started" and st["supervisor_alive"] is False and st["mission"]["complete"] is True


# ------------------------------------------------------------------ departments the director creates

def test_the_director_can_create_a_department_that_works_and_survives_a_restart(tmp_path):
    cfg = cfg_for(tmp_path)
    dept = dict(type="create_department", name="Reverse Engineering", mission="owns binary analysis",
                writes_code=True, resource_leases=True)
    script = [j(say="new team", actions=[dept, {**TICKET, "department": "reverse_engineering", "title": "Map the renderer"}]),
              j(action="delegate", tickets=[WORKER]), j(action="done", result={}), j(action="done", result={"mapped": 1})]
    ctl = Ctl(cfg, cfg["workspace"], MockProvider(script))
    ctl.director.event_turn("start", "plan")
    assert "reverse_engineering" in ctl.director.managers
    spec = cfg["departments"]["reverse_engineering"]
    assert spec["writes_code"] is True and "resource_leases" not in spec           # hardware is never granted
    assert ctl.director.managers["research"].departments["reverse_engineering"] == "reverse_engineering_manager"    # others can file work for it
    ctl.run()
    assert [t["status"] for t in ctl.store.list_tickets() if t["parent_id"] is None] == ["done"]
    ctl.close()
    cfg2 = cfg_for(tmp_path / "again")                  # a fresh process, same workspace: the department is still there
    cfg2["workspace"] = cfg["workspace"]
    ctl2 = Ctl(cfg2, cfg["workspace"], MockProvider([]))
    assert "reverse_engineering" in ctl2.director.managers
    ctl2.close()


def test_bad_departments_are_refused_and_retiring_needs_an_idle_one(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["policies"]["max_departments"] = 2
    ctl = Ctl(cfg, cfg["workspace"], MockProvider([]))
    with pytest.raises(ValueError, match="already exists"):
        ctl.director.add_department("research", "x")
    with pytest.raises(ValueError, match="name"):
        ctl.director.add_department("Bad/Name!", "x")
    with pytest.raises(ValueError, match="mission"):
        ctl.director.add_department("docs", "  ")
    ctl.director.add_department("docs", "writes the docs")
    with pytest.raises(ValueError, match="retire one"):
        ctl.director.add_department("third", "x")
    with pytest.raises(ValueError, match="human's"):
        ctl.director.retire_department("research")
    ctl.create(dict(department="docs", title="t", context="c", acceptance_criteria=["a"],
                    return_spec={"format": "f", "description": "d"}))
    with pytest.raises(ValueError, match="unfinished"):
        ctl.director.retire_department("docs")
    assert "docs" in ctl.department("list")["departments"]
    ctl.close()


def test_launchd_plist_and_mac_keep_awake(monkeypatch, tmp_path):
    import plistlib
    plist = plistlib.loads(winsys.launchd_plist(tmp_path).encode())
    assert plist["KeepAlive"] is True and plist["ProgramArguments"][-1] == "supervise"
    calls = []

    class P:
        def terminate(self):
            calls.append("t")
    monkeypatch.setattr(winsys.sys, "platform", "darwin")
    monkeypatch.setattr(winsys.subprocess, "Popen", lambda args, **k: calls.append(args) or P())
    assert winsys.keep_awake() is True and calls[0][0] == "caffeinate"
    winsys.allow_sleep()
    assert calls[-1] == "t"
