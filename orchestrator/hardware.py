"""Background jobs for long-running experiments, sweeps and monitors.

Experiment workers do not drive hardware step by step; they request or write a Python tool,
start it as a job, then oversee it (status / tail / wait) and fix it when it breaks.
Jobs are plain subprocesses (no shell) running a workspace script with the tool venv's Python.
Output goes to workspace/jobs/<id>/log.txt. There is no sandbox: scripts can do anything the
user's account can do, by design (the human prepared the harness).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .store import NotFound, Store, StoreError

MAX_WAIT = 60.0


def venv_python(workspace: str | Path) -> Path | None:
    base = Path(workspace) / "tools" / "venv"
    for rel in ("Scripts/python.exe", "bin/python"):
        if (base / rel).exists():
            return base / rel
    return None


def python_exe(workspace: str | Path) -> str:
    v = venv_python(workspace)
    return str(v) if v else sys.executable


def resolve_in_workspace(workspace: str | Path, rel: str) -> Path:
    root = Path(workspace).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise StoreError(f"path escapes workspace: {rel}")
    return p


class JobRunner:
    def __init__(self, store: Store, workspace: str | Path) -> None:
        self.store = store
        self.workspace = Path(workspace)
        self._procs: dict[str, subprocess.Popen] = {}

    def start(self, ticket_id: str, actor: str, script: str, args: list[str] | None = None,
              name: str | None = None, resource: str | None = None) -> dict[str, Any]:
        path = resolve_in_workspace(self.workspace, script)
        if not path.is_file():
            raise StoreError(f"script not found: {script}")
        if resource is not None:
            res = next((r for r in self.store.list_resources() if r["name"] == resource), None)
            if res is None:
                raise NotFound(f"resource {resource}")
            if res["holder_ticket"] != ticket_id:
                raise StoreError(f"claim {resource} before starting a job that uses it")
        job_id = uuid.uuid4().hex[:10]
        log_dir = self.workspace / "jobs" / job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "log.txt"
        cmd = [python_exe(self.workspace), "-u", str(path), *[str(a) for a in (args or [])]]
        flags: dict[str, Any] = {}
        if os.name == "nt":
            flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            flags["start_new_session"] = True
        with log_path.open("wb") as log:
            proc = subprocess.Popen(cmd, cwd=self.workspace, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **flags)
        self._procs[job_id] = proc
        return self.store.create_job(ticket_id, actor, name or path.stem, cmd, str(log_path),
                                     resource=resource, pid=proc.pid, job_id=job_id)

    def refresh(self, job_id: str, actor: str = "jobs") -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["state"] != "running":
            return job
        proc = self._procs.get(job_id)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                return job
            return self.store.update_job(job_id, actor, state="exited", exit_code=rc, ended_at=now)
        # Started by an earlier process: we can only tell whether the pid is still alive.
        alive = job["pid"] is not None and psutil.pid_exists(job["pid"]) and \
            psutil.Process(job["pid"]).status() != psutil.STATUS_ZOMBIE
        if alive:
            return job
        return self.store.update_job(job_id, actor, state="lost", ended_at=now)

    def refresh_all(self) -> list[dict[str, Any]]:
        return [self.refresh(j["id"]) for j in self.store.list_jobs(state="running")]

    def tail(self, job_id: str, lines: int = 40, max_chars: int = 6000) -> str:
        job = self.store.get_job(job_id)
        p = Path(job["log_path"])
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])[-max_chars:]

    def wait(self, job_id: str, seconds: float) -> dict[str, Any]:
        """Block up to `seconds` (capped) or until the job leaves 'running'."""
        deadline = time.monotonic() + max(0.0, min(float(seconds), MAX_WAIT))
        job = self.refresh(job_id)
        while job["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.2)
            job = self.refresh(job_id)
        return job

    def stop(self, job_id: str, actor: str) -> dict[str, Any]:
        job = self.refresh(job_id, actor)
        if job["state"] != "running":
            return job
        try:
            parent = psutil.Process(job["pid"])
            procs = [*parent.children(recursive=True), parent]
            for p in procs:
                p.terminate()
            _, alive = psutil.wait_procs(procs, timeout=5)
            for p in alive:
                p.kill()
        except psutil.NoSuchProcess:
            pass
        proc = self._procs.get(job_id)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return self.store.update_job(job_id, actor, state="stopped",
                                     ended_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))


def hardware_brief(store: Store) -> str:
    """Compact status the Experiment manager reads before deciding anything."""
    lines = []
    for r in store.list_resources():
        lines.append(f"- {r['name']}: {r['state']}"
                     + (f" (held by {r['holder_ticket']}, expected {r['expected_duration']})" if r["holder_ticket"] else "")
                     + (f" | safe to stop: {r['safe_to_stop']}" if r["safe_to_stop"] else ""))
    for j in store.list_jobs(state="running"):
        lines.append(f"- job {j['id']} '{j['name']}' running (ticket {j['ticket_id']}, resource {j['resource'] or '-'})")
    return "\n".join(lines)
