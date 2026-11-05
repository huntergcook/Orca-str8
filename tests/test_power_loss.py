"""Simulated crash / power loss: a restarted manager must get back to where the project was."""
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.dashboard import create_app
from orchestrator.memory import Memory
from orchestrator.providers import MockProvider
from orchestrator.util import atomic_write, parse_stamp, process_alive, process_stamp

BASE = dict(worker_role="r", context="c", acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"})
SPEC = dict(department="research", title="Survey", context="c", acceptance_criteria=["a"],
            return_spec={"format": "f", "description": "d"})
DEAD = "999999999:1.00"                      # a pid that does not exist


def j(**kw):
    return json.dumps(kw)


def make(tmp_path, script=()):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    return Ctl(cfg, tmp_path, MockProvider(list(script)))


def crashed_project(tmp_path):
    """Leave a workspace exactly as a power cut mid-run would: lock file, in-progress tickets, a worker that
    started but never finished, a claimed resource and a 'running' job whose process is gone."""
    ctl = make(tmp_path)
    top = ctl.create(SPEC)["ticket_id"]
    st = ctl.store
    st.transition(top, "in_progress", "research_manager")
    kid = st.create_ticket({**BASE, "title": "Worker", "assignee": "worker:r-1"}, "research_manager",
                           parent_id=top)["id"]
    st.transition(kid, "in_progress", "worker:r-1")
    st.add_event(kid, "worker:r-1", "worker_started", {"resumed_after_interruption": False})
    st.register_resource("rig", "stop between sweeps")
    st.claim_resource("rig", kid, "20m", actor="worker:r-1")
    st.create_job(kid, "worker:r-1", "sweep", ["python", "sweep.py"], str(tmp_path / "log.txt"),
                  resource="rig", pid=999999999, pid_started=1.0)
    (tmp_path / "RUNNING").write_text(DEAD)                 # the run that died
    ctl.close()
    return top, kid


def test_a_new_session_sees_everything_that_was_interrupted(tmp_path):
    top, kid = crashed_project(tmp_path)
    ctl = make(tmp_path)                                    # "re-opening the manager" after the power cut
    r = ctl.resume()["recovery"]
    assert r["needed"] and r["stale_lock"] and r["run_active"] is False
    assert {t["id"] for t in r["unfinished_tickets"]} == {top, kid}
    assert [x["name"] for x in r["lost_jobs"]] == ["sweep"]
    assert r["orphaned_resources"][0]["name"] == "rig" and "verify the hardware" in r["orphaned_resources"][0]["warning"]
    assert r["last_activity"]
    ctl.close()


def test_recover_cleans_up_but_never_releases_hardware(tmp_path):
    top, kid = crashed_project(tmp_path)
    ctl = make(tmp_path)
    r = ctl.recover()
    assert not (tmp_path / "RUNNING").exists()                              # stale lock cleared
    assert ctl.store.list_jobs()[0]["state"] == "lost"                      # dead job recorded honestly
    assert ctl.store.list_resources()[0]["state"] == "busy"                 # hardware NOT auto-released
    assert r["orphaned_resources"]
    ctl.release_resource("rig", "checked: rig is off and cool")             # explicit, human-confirmed
    assert ctl.store.list_resources()[0]["state"] == "free"
    assert any(e["type"] == "resource_force_released" for e in ctl.store.events(kid))
    assert ctl.recovery()["orphaned_resources"] == []
    ctl.close()


def test_run_continues_the_interrupted_work_and_the_worker_knows(tmp_path):
    top, kid = crashed_project(tmp_path)
    ctl = make(tmp_path, [j(action="done", result={"found": "it"}),         # restarted worker finishes
                          j(action="done", result={"final": 1})])          # manager reviews
    ctl.recover()
    out = ctl.run()
    assert next(t for t in out["tickets"] if t["id"] == top)["status"] == "done"
    prompt = ctl.provider.inner.calls[0]["messages"][0]["content"]
    assert "interrupted (crash, power loss or step limit)" in prompt and f"work/{kid}/" in prompt   # told to check saved files
    started = [e for e in ctl.store.events(kid) if e["type"] == "worker_started"]
    assert [e["payload"]["resumed_after_interruption"] for e in started] == [False, True]
    ctl.close()


def test_a_normal_retry_is_not_mistaken_for_an_interruption(tmp_path):
    ctl = make(tmp_path, [j(action="escalate", reason="stuck"), j(action="done", result={})])
    st = ctl.store
    top = ctl.create(SPEC)["ticket_id"]
    st.transition(top, "in_progress", "research_manager")
    kid = st.create_ticket({**BASE, "title": "W", "assignee": "worker:r-1"}, "research_manager", parent_id=top)["id"]
    from orchestrator.agent import run_ticket
    run_ticket(st, ctl.provider, kid, "worker:r-1", "role", ["m"])              # escalates
    from orchestrator import actions
    actions.retry(st, kid, "try again", "human")
    run_ticket(st, ctl.provider, kid, "worker:r-1", "role", ["m"])              # retried normally
    assert "interrupted" not in ctl.provider.inner.calls[1]["messages"][0]["content"]
    ctl.close()


# ----------------------------------------------------------- process identity

def test_a_reused_pid_after_reboot_is_not_mistaken_for_the_old_run(tmp_path):
    ctl = make(tmp_path)
    (tmp_path / "RUNNING").write_text(f"{os.getpid()}:1.00")           # same pid, but a different start time
    assert ctl.run_active() is False
    (tmp_path / "RUNNING").write_text(process_stamp())                 # this very process
    assert ctl.run_active() is True
    (tmp_path / "RUNNING").write_text(str(os.getpid()))                # older lock format (bare pid) still works
    assert ctl.run_active() is True
    (tmp_path / "RUNNING").write_text("garbage")
    assert ctl.run_active() is False
    assert process_alive(os.getpid()) and not process_alive(999999999) and not process_alive(None)
    assert parse_stamp("12:34.5") == (12, 34.5) and parse_stamp("nonsense") == (None, None)
    ctl.close()


def test_run_refuses_while_a_real_run_holds_the_lock_and_cleans_up(tmp_path):
    ctl = make(tmp_path, [j(action="done", result={})])
    ctl.create(SPEC)
    (tmp_path / "RUNNING").write_text(process_stamp())
    with pytest.raises(Exception, match="already active"):
        ctl.run()
    (tmp_path / "RUNNING").write_text(DEAD)                            # stale lock from the power cut
    ctl.run()
    assert not (tmp_path / "RUNNING").exists()
    ctl.close()


# ------------------------------------------------------------- durable files

def test_atomic_write_replaces_whole_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "sub" / "state.md"
    atomic_write(p, "first")
    atomic_write(p, "second version")
    assert p.read_text() == "second version"
    assert [f.name for f in p.parent.iterdir()] == ["state.md"]


def test_memory_and_human_log_survive_a_restart(tmp_path):
    ctl = make(tmp_path)
    ctl.state("GOAL: map the drift")
    ctl.log("decided to use pyserial")
    ctl.human_said("I want quick-and-dirty, finish by Friday")
    ctl.close()
    again = make(tmp_path)                                              # fresh session reading only the files
    r = again.resume()
    assert "map the drift" in r["state_notes"] and "pyserial" in r["journal_tail"]
    assert "quick-and-dirty, finish by Friday" in r["human_log_tail"]
    again.close()
    assert Memory(tmp_path, "director").read_state() == "GOAL: map the drift"


def test_old_databases_get_the_new_jobs_column(tmp_path):
    db = tmp_path / "orchestrator.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor TEXT NOT NULL, name TEXT NOT NULL,"
              " command TEXT NOT NULL, resource TEXT, pid INTEGER, state TEXT NOT NULL DEFAULT 'running',"
              " exit_code INTEGER, log_path TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT)")
    c.commit()
    c.close()
    s = Store(db)
    assert "pid_started" in {r[1] for r in s._conn.execute("PRAGMA table_info(jobs)")}
    s.close()
    Store(db).close()                                                   # reopening is idempotent


def test_every_commit_is_fully_synced(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 2     # FULL
    s.close()


# ----------------------------------------------------------------- dashboard

def test_dashboard_offers_resume_after_a_restart(tmp_path):
    crashed_project(tmp_path)
    launched = []
    c = TestClient(create_app(tmp_path / "orchestrator.db", load_config(None), launch_run=lambda: launched.append(1),
                              project=tmp_path, token="tok"), base_url="http://127.0.0.1")
    s = c.get("/api/state").json()
    assert s["run_active"] is False and [u["status"] for u in s["unfinished"]] == ["in_progress"]
    assert c.post("/api/resume").status_code == 403                                  # token required
    ok = c.post("/api/resume", headers={"x-dashboard-token": "tok"})
    assert ok.status_code == 200 and launched == [1]
    (tmp_path / "RUNNING").write_text(process_stamp())                              # now a run is alive
    assert c.get("/api/state").json()["unfinished"] == []
    assert c.post("/api/resume", headers={"x-dashboard-token": "tok"}).status_code == 409
    assert launched == [1]
