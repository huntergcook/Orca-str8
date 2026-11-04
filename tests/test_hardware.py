import json
import sys
import time

import pytest

from orchestrator import Store
from orchestrator.agent import run_ticket
from orchestrator.hardware import JobRunner, hardware_brief
from orchestrator.providers import MockProvider
from orchestrator.tools import ToolBox, ToolRegistry


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def ticket(store, assignee="worker:x"):
    return store.create_ticket(dict(
        title="T", assignee=assignee, worker_role="experiment_runner", context="c", acceptance_criteria=["a"],
        return_spec={"format": "f", "description": "d"}), actor="experiment_manager")["id"]


def box(store, ws, tid, **kw):
    kw.setdefault("can_use_hardware", True)
    return ToolBox(ws, "worker:x", store=store, ticket_id=tid, **kw)


def test_file_tools_confined(store, tmp_path):
    b = box(store, tmp_path, ticket(store))
    assert b.call("write_file", {"path": "work/a.py", "content": "print(1)"})["ok"]
    assert b.call("read_file", {"path": "work/a.py"})["content"] == "print(1)"
    assert "escapes" in b.call("read_file", {"path": "../secret"})["error"]
    assert "escapes" in b.call("write_file", {"path": "../x.py", "content": ""})["error"]
    for protected in ("memory/top_manager/state.md", "orchestrator.db", "PAUSE", "jobs/x/log.txt"):
        assert "managed by the orchestrator" in b.call("write_file", {"path": protected, "content": "x"})["error"]


def test_run_python_runs_script(store, tmp_path):
    b = box(store, tmp_path, ticket(store))
    b.call("write_file", {"path": "work/hello.py", "content": "import sys; print('hi', sys.argv[1])"})
    r = b.call("run_python", {"script": "work/hello.py", "args": ["there"]})
    assert r["ok"] and r["exit_code"] == 0 and "hi there" in r["stdout"]


def test_pip_install_validates_and_registers(store, tmp_path):
    calls = []

    def fake_run(args, timeout=60, cwd=None):
        calls.append(args)
        if "venv" in args:                                          # pretend the venv was created
            exe = tmp_path / "tools" / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
            exe.mkdir(parents=True, exist_ok=True)
            (exe / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
        return 0, "Successfully installed pyserial", ""

    b = ToolBox(tmp_path, "tool_scout", can_acquire=True, store=store, run_cmd=fake_run)
    for bad in ("--index-url=http://evil", "-r", "a b"):
        assert not b.call("pip_install", {"spec": bad})["ok"]
    assert calls == []                                              # nothing ran for rejected specs
    r = b.call("pip_install", {"spec": "pyserial>=3.5", "description": "serial port access", "tags": ["hardware"]})
    assert r["ok"], r
    assert "venv" in calls[0]                                       # venv created first
    assert calls[-1][1:4] == ["-m", "pip", "install"] and calls[-1][-1] == "pyserial>=3.5"
    assert ToolRegistry(tmp_path).get("pyserial")["kind"] == "pip"
    r2 = b.call("pip_install", {"spec": "git+https://github.com/a/some-tool.git"})
    assert r2["ok"] and ToolRegistry(tmp_path).get("some-tool")


def test_pypi_info_and_restrictions(store, tmp_path):
    b = ToolBox(tmp_path, "tool_scout", can_acquire=True,
                http_get=lambda url: {"info": {"name": "pyserial", "version": "3.5", "summary": "s", "license": "BSD"}})
    assert b.call("pypi_info", {"name": "pyserial"})["version"] == "3.5"
    plain = ToolBox(tmp_path, "w", store=store, ticket_id=ticket(store))
    assert "restricted" in plain.call("pip_install", {"spec": "x"})["error"]
    assert "Experiment" in plain.call("claim_resource", {"name": "r"})["error"]


def test_claim_job_wait_tail(store, tmp_path):
    store.register_resource("scope", "stop between sweep points")
    tid = ticket(store)
    runner = JobRunner(store, tmp_path)
    b = box(store, tmp_path, tid, jobs=runner)
    b.call("write_file", {"path": "work/sweep.py", "content": "print('point 1'); print('point 2')"})

    assert "claim" in b.call("start_job", {"script": "work/sweep.py", "resource": "scope"})["error"]
    assert b.call("claim_resource", {"name": "scope", "expected_duration": "1m"})["ok"]
    started = b.call("start_job", {"script": "work/sweep.py", "resource": "scope"})
    assert started["ok"]
    done = b.call("wait_job", {"job_id": started["job_id"], "seconds": 20})
    assert done["state"] == "exited" and done["exit_code"] == 0 and "point 2" in done["log_tail"]
    assert "point 1" in b.call("job_tail", {"job_id": started["job_id"]})["log"]
    assert b.call("release_resource", {"name": "scope"})["ok"]
    types = [e["type"] for e in store.events(tid)]
    assert "job_started" in types and "job_exited" in types and "resource_released" in types


def test_cannot_release_while_job_running_and_stop_job(store, tmp_path):
    store.register_resource("rig")
    tid = ticket(store)
    runner = JobRunner(store, tmp_path)
    b = box(store, tmp_path, tid, jobs=runner)
    b.call("write_file", {"path": "work/long.py", "content": "import time\nprint('start', flush=True)\ntime.sleep(60)"})
    b.call("claim_resource", {"name": "rig"})
    job = b.call("start_job", {"script": "work/long.py", "resource": "rig"})["job_id"]
    assert "still using" in b.call("release_resource", {"name": "rig"})["error"]
    assert "rig: busy" in hardware_brief(store) and "running" in hardware_brief(store)
    time.sleep(0.5)
    assert b.call("stop_job", {"job_id": job})["state"] == "stopped"
    assert b.call("release_resource", {"name": "rig"})["ok"]
    assert runner.refresh(job)["state"] == "stopped"


def test_job_failure_is_visible(store, tmp_path):
    tid = ticket(store)
    b = box(store, tmp_path, tid, jobs=JobRunner(store, tmp_path))
    b.call("write_file", {"path": "work/bad.py", "content": "raise SystemExit(3)"})
    j = b.call("start_job", {"script": "work/bad.py"})["job_id"]
    assert b.call("wait_job", {"job_id": j, "seconds": 20})["exit_code"] == 3


def test_worker_oversees_job_end_to_end(store, tmp_path):
    store.register_resource("rig")
    tid = ticket(store)
    runner = JobRunner(store, tmp_path)
    b = box(store, tmp_path, tid, jobs=runner)
    j = lambda **kw: json.dumps(kw)
    p = MockProvider([
        j(action="tool", tool="write_file", args={"path": "work/s.py", "content": "print('ok')"}),
        j(action="tool", tool="claim_resource", args={"name": "rig"}),
        j(action="tool", tool="start_job", args={"script": "work/s.py", "resource": "rig", "name": "sweep"}),
    ])
    # the worker needs the real job id for wait/release, so script the rest after starting
    import orchestrator.agent as agent
    orig = p.complete
    state = {}

    def smart(**kw):
        n = len(p.calls)
        if n == 3:
            state["job"] = runner.store.list_jobs()[0]["id"]
            p.script.extend([j(action="tool", tool="wait_job", args={"job_id": state["job"], "seconds": 20}),
                             j(action="tool", tool="release_resource", args={"name": "rig"}),
                             j(action="done", result={"sweep": "complete"})])
        return orig(**kw)

    p.complete = smart
    t = run_ticket(store, p, tid, "worker:x", "role", ["m"], toolbox=b)
    assert t["status"] == "done" and t["result"] == {"sweep": "complete"}
    assert store.list_resources()[0]["state"] == "free"
    assert "claim_resource" in p.calls[0]["system"]
