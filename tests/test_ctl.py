import json

import pytest

from orchestrator.config import load_config
from orchestrator.ctl import Ctl, main
from orchestrator.providers import MockProvider
from orchestrator.smoke import configured_models


def j(**kw):
    return json.dumps(kw)


SPEC = dict(department="research", title="Survey X", context="find papers",
            acceptance_criteria=["3 papers"], return_spec={"format": "summary", "description": "d"})
WORKER = dict(title="W", worker_role="surveyor", context="c", acceptance_criteria=["a"],
              return_spec={"format": "f", "description": "d"})


def make(tmp_path, script):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    cfg["workspace"] = str(tmp_path)
    return Ctl(cfg, tmp_path, MockProvider(script)), cfg


def test_create_run_board_end_to_end(tmp_path):
    ctl, _ = make(tmp_path, [j(action="delegate", tickets=[WORKER]),
                             j(action="done", result={"s": 1}),
                             j(action="done", result={"final": 1})])
    tid = ctl.create(SPEC)["ticket_id"]
    out = ctl.run()
    assert out["changed"] == [{"id": tid, "title": "Survey X", "from": "open", "to": "done"}]
    row = out["tickets"][0]
    assert row["result"] == {"final": 1} and row["children"] == {"done": 1}
    assert ctl.ticket(tid)["children"][0]["status"] == "done"
    ctl.close()


def test_unknown_department_rejected(tmp_path):
    ctl, _ = make(tmp_path, [])
    with pytest.raises(Exception, match="unknown department"):
        ctl.create({**SPEC, "department": "nope"})
    ctl.close()


def test_attention_answer_and_retry(tmp_path):
    ctl, _ = make(tmp_path, [
        j(action="ask", question="human, which budget?"),                 # manager asks upward at planning
        j(action="delegate", tickets=[WORKER]),                           # after the answer, plans
        j(action="escalate", reason="unsafe to proceed"),                 # worker escalates
        j(action="ask", question="worker is blocked; how to proceed?"),   # manager reviews, asks upward
    ])
    tid = ctl.create(SPEC)["ticket_id"]
    out = ctl.run()
    att = out["needs_attention"]
    assert att[0]["id"] == tid and att[0]["open_question"] == "human, which budget?" and att[0]["level"] == "top"
    assert att[0]["route"] == "top_manager"                      # a manager's question goes to the top manager
    assert ctl.answer(tid, "$10")["status"] == "in_progress"
    out = ctl.run()
    # the worker's escalation stays with its manager (who reviewed it and asked upward); only the
    # manager's question reaches the top manager's list
    assert [a["level"] for a in out["needs_attention"]] == ["top"]
    assert out["managers_handling"] and out["managers_handling"][0]["status"] == "escalated"
    ctl.close()


def test_retry_appends_guidance_and_reopens(tmp_path):
    ctl, _ = make(tmp_path, [j(action="escalate", reason="stuck")])
    tid = ctl.create(SPEC)["ticket_id"]
    ctl.run()
    assert ctl.ticket(tid)["ticket"]["status"] == "escalated"
    assert ctl.retry(tid, "use the 2020 dataset")["status"] == "in_progress"
    assert "use the 2020 dataset" in ctl.ticket(tid)["ticket"]["context"]
    ctl.close()


def test_state_log_resume_finish_pause(tmp_path):
    ctl, _ = make(tmp_path, [])
    ctl.state("GOAL: map the drift\nSTOP: when plot reproduces")
    ctl.log("human prefers quick-and-dirty")
    r = ctl.resume()
    assert "map the drift" in r["state_notes"] and "quick-and-dirty" in r["journal_tail"]
    assert r["departments"] == ["research"] and r["finished"] is False and r["halted"] is None
    ctl.pause(True)
    assert "paused" in ctl.board()["halted"]
    ctl.pause(False)
    assert ctl.board()["halted"] is None
    ctl.finish("done and dusted")
    assert ctl.resume()["finished"] is True
    ctl.close()


def test_run_reports_halt_without_crashing(tmp_path):
    ctl, _ = make(tmp_path, [j(action="done", result={})])
    ctl.create(SPEC)
    ctl.pause(True)
    out = ctl.run()
    assert "paused" in out["halted"]
    ctl.close()


def test_cli_main_roundtrip(tmp_path, capsys, monkeypatch):
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text(f"workspace: {json.dumps(str(tmp_path / 'ws'))}\ndepartments:\n  research: {{}}\n")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(SPEC)))
    assert main(["--config", str(cfgfile), "create"], provider=MockProvider([])) == 0
    created = json.loads(capsys.readouterr().out)
    assert main(["--config", str(cfgfile), "board"], provider=MockProvider([])) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["tickets"][0]["id"] == created["ticket_id"]
    assert main(["--config", str(cfgfile), "ticket", "nope"], provider=MockProvider([])) == 1


def test_smoke_skips_external_tier():
    cfg = load_config(None)
    cfg["models"]["top"] = {"provider": "external", "model": "claude-code"}
    cfg["models"]["manager"] = {"provider": "deepseek", "model": "deepseek-v4-pro"}
    cfg["models"]["worker"] = {"provider": "deepseek", "model": "deepseek-flash"}
    cfg["verification"]["verifier_models"] = ["deepseek-v4-pro", "deepseek-flash"]
    assert configured_models(cfg) == ["deepseek-v4-pro", "deepseek-flash"]


# ---- robustness: crashes must be visible, keys checked up front, runs locked -------------

def test_provider_crash_escalates_instead_of_hanging(tmp_path):
    # MockProvider with an empty script raises mid-run, like a bad key or API outage would.
    ctl, _ = make(tmp_path, [j(action="delegate", tickets=[WORKER])])     # then the worker call explodes
    tid = ctl.create(SPEC)["ticket_id"]
    out = ctl.run()                                    # must return, not raise
    row = next(t for t in out["tickets"] if t["id"] == tid)
    assert row["status"] == "escalated"
    kids = ctl.ticket(tid)["children"]
    assert kids and all(k["status"] == "escalated" for k in kids)
    assert any(e["type"] == "run_error" for e in ctl.ticket(tid)["events"])
    why = [a["why"] for a in out["needs_attention"] if a["id"] == tid][0]
    assert "run error" in str(why)
    ctl.close()


def test_missing_key_fails_fast_with_location(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    cfg["models"]["top"] = {"provider": "external", "model": "claude-code"}
    cfg["models"]["manager"] = {"provider": "deepseek", "model": "deepseek-v4-pro"}
    cfg["models"]["worker"] = {"provider": "deepseek", "model": "deepseek-flash"}
    cfg["verification"]["enabled"] = False
    ctl = Ctl(cfg, tmp_path)                            # real (unused) provider: key check applies
    assert ctl.missing_keys() == ["DEEPSEEK_API_KEY"]
    assert ctl.resume()["missing_keys"] == ["DEEPSEEK_API_KEY"]
    ctl.create(SPEC)
    with pytest.raises(Exception, match=r"missing API key.*DEEPSEEK_API_KEY.*.env"):
        ctl.run()
    assert ctl.store.list_tickets()[0]["status"] == "open"       # nothing was started
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert ctl.missing_keys() == []
    ctl.close()


def test_run_lock_blocks_second_run_and_clears(tmp_path):
    import os
    ctl, _ = make(tmp_path, [j(action="done", result={})])
    ctl.create(SPEC)
    ctl.lock_path.write_text(str(os.getpid()))          # pretend a live run owns the lock
    assert ctl.board()["run_active"] is True
    with pytest.raises(Exception, match="already active"):
        ctl.run()
    ctl.lock_path.write_text("999999999")               # stale lock from a dead process
    assert ctl.board()["run_active"] is False
    ctl.run()
    assert not ctl.lock_path.exists() and ctl.board()["run_active"] is False
    ctl.close()


def test_resume_warns_when_workspace_is_inside_orchestrator_repo():
    from orchestrator.ctl import ORCH_HOME
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    ctl = Ctl(cfg, ORCH_HOME / "workspace_test_tmp", MockProvider([]))
    try:
        assert "orchestrator repo" in ctl.resume()["warning"]
    finally:
        ctl.close()
        import shutil
        shutil.rmtree(ORCH_HOME / "workspace_test_tmp", ignore_errors=True)
