"""Control interface for an EXTERNAL top manager (e.g. an interactive Claude Code session).

The external agent plays the top manager and talks to the human; this tool is its hands:
it creates tickets, runs the department managers (workers/verifiers run on the configured
API providers), reads results, answers questions and keeps durable notes. All state is in
files and SQLite, so a fresh session can resume with `resume`.

    python -m orchestrator.ctl [--config PATH] <command> [args]      (JSON on stdout)

Commands: resume | board | ticket ID | create [--file F | stdin JSON] | run | watch | answer ID TEXT |
          retry ID [TEXT] | cancel ID [TEXT] | handoff ID [TEXT] | recover | human TEXT |
          release-resource NAME [REASON] | state [--set (stdin)] | log TEXT | finish SUMMARY | pause | unpause
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import actions
from .config import enter_project, load_config, model_chain
from .hardware import hardware_brief
from .limits import GuardedProvider, Halted
from .memory import human_log
from .util import append_durable, atomic_write, process_alive, process_stamp
from .providers import Provider, build_provider
from .store import Store, StoreError
from .top import TopManager

ORCH_HOME = Path(__file__).resolve().parents[1]


class Ctl:
    def __init__(self, cfg: dict[str, Any], workspace: str | Path, provider: Provider | None = None) -> None:
        self.cfg, self.workspace = cfg, Path(workspace)
        self._custom_provider = provider is not None     # tests inject mocks: no real keys needed
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.workspace / "orchestrator.db")
        self.provider = GuardedProvider(provider or build_provider(cfg), self.store, cfg, self.workspace)
        self.top = TopManager(self.store, self.provider, cfg, self.workspace)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ reading

    def _row(self, t: dict[str, Any], full: bool = False) -> dict[str, Any]:
        kids = self.store.list_tickets(parent_id=t["id"])
        row = {"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
               "rigor": t.get("rigor", "balanced"), "children": {k["status"]: 0 for k in kids}}
        for k in kids:
            row["children"][k["status"]] += 1
        if t["result"] is not None:
            row["result"] = t["result"]
        q = [x["question"] for x in t["questions"] if "answer" not in x]
        if q:
            row["open_question"] = q[0]
        if t.get("verification_notes"):
            row["verification_notes"] = t["verification_notes"]
        return row

    def board(self) -> dict[str, Any]:
        top_level = [t for t in self.store.list_tickets() if t["parent_id"] is None]
        rows = [self._row(t) for t in top_level]
        attention, handled_below = [], []
        for t in self.store.list_tickets():
            route = actions.route_of(t)
            if route is None:
                continue
            item = {**self._row(t), "level": "top" if t["parent_id"] is None else "worker", "route": route,
                    "why": actions.why_from_events(self.store.events(t["id"]))}
            # needs_attention is only what the department managers could not handle (or what is the
            # human's call by policy). Worker problems are the managers' to fix and are listed separately.
            (handled_below if route == "manager" else attention).append(item)
        return {"tickets": rows, "needs_attention": attention,
                "managers_handling": [{k: i[k] for k in ("id", "title", "status", "why")} for i in handled_below],
                "halted": self._halt_reason(), "run_active": self.run_active()}

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        t = self.store.get_ticket(ticket_id)
        return {"ticket": t, "children": [self._row(k) for k in self.store.list_tickets(parent_id=ticket_id)],
                "events": [e for e in self.store.events(ticket_id) if e["type"] != "usage"][-25:]}

    def _halt_reason(self) -> str | None:
        try:
            self.provider.check()
            return None
        except Halted as h:
            return str(h)

    def resume(self) -> dict[str, Any]:
        """Everything a fresh top manager needs to pick up the work."""
        log = human_log(self.workspace)
        return {"departments": list(self.top.managers),
                "state_notes": self.top.memory.read_state() or "(none yet)",
                "journal_tail": self.top.memory.read_journal(3000),
                "human_log_tail": log.read_text(encoding="utf-8")[-3000:] if log.exists() else "",
                "hardware": hardware_brief(self.store) or "(none)",
                "finished": (self.workspace / "FINISHED").exists(),
                "missing_keys": self.missing_keys(), "recovery": self.recovery(), **self.where(), **self.board()}

    # ------------------------------------------------------------------ acting

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        spec = {"worker_role": "worker", **spec}
        tid = self.top.create_ticket(spec)
        if tid is None:
            raise StoreError(f"unknown department {spec.get('department')!r}; have {list(self.top.managers)}")
        return {"ticket_id": tid}

    # ---------------------------------------------------------- run safety

    @property
    def lock_path(self) -> Path:
        return self.workspace / "RUNNING"

    def run_active(self) -> bool:
        """True if a `run` process is alive (the lock file holds a live pid)."""
        return actions.run_is_active(self.workspace)

    def missing_keys(self) -> list[str]:
        """API key variables the configured models need but the environment lacks."""
        if self._custom_provider:
            return []
        provider = build_provider(self.cfg)
        models: list[str] = []
        for tier in ("manager", "worker"):
            if self.cfg["models"][tier].get("provider") != "external":
                models += model_chain(self.cfg, tier)
        if self.cfg["verification"].get("enabled"):
            models += list(self.cfg["verification"].get("verifier_models") or [])
        need: list[str] = []
        for m in dict.fromkeys(models):
            name = provider.provider_name(m)
            var = self.cfg["providers"].get(name, {}).get("api_key_env")
            if var and not os.environ.get(var) and var not in need:
                need.append(var)
        return need

    def where(self) -> dict[str, Any]:
        ws = self.workspace.resolve()
        info: dict[str, Any] = {"workspace": str(ws), "cwd": str(Path.cwd())}
        if ORCH_HOME in ws.parents:
            info["warning"] = ("this workspace is inside the orchestrator repo itself; run ctl from your "
                               "project folder (where your config/orchestrator.yaml and .env live)")
        return info

    def run(self) -> dict[str, Any]:
        """Run department managers on all open/in-progress tickets. May take a long time."""
        miss = self.missing_keys()
        if miss:
            raise StoreError(f"missing API key(s): {', '.join(miss)}. Put them in {Path.cwd() / '.env'} or the "
                             f"environment. Working dir: {Path.cwd()}; workspace: {self.workspace.resolve()}")
        if self.run_active():
            raise StoreError(f"a run is already active (pid {self.lock_path.read_text().strip()}); "
                             "watch `board` instead of starting another")
        atomic_write(self.lock_path, process_stamp())      # pid + start time: stays correct across reboots
        try:
            return self._run()
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _run(self) -> dict[str, Any]:
        before = {t["id"]: t["status"] for t in self.store.list_tickets()}
        halted = None
        try:
            self.top.pump()
        except Halted as h:
            halted = str(h)
        after = self.board()
        changed = [{"id": t["id"], "title": t["title"], "from": before.get(t["id"]), "to": t["status"]}
                   for t in self.store.list_tickets()
                   if t["parent_id"] is None and before.get(t["id"]) != t["status"]]
        return {"halted": halted, "changed": changed, **after}

    def answer(self, ticket_id: str, text: str) -> dict[str, Any]:
        return actions.answer(self.store, ticket_id, text, "top_manager")

    def retry(self, ticket_id: str, text: str = "") -> dict[str, Any]:
        """Send an escalated ticket back to work (guidance is appended to its context)."""
        return actions.retry(self.store, ticket_id, text, "top_manager")

    # ------------------------------------------------------- crash / power-loss recovery

    def recovery(self, apply: bool = False) -> dict[str, Any]:
        """What a crash or power cut left behind, so a restarted top manager can pick the whole project
        back up. With apply=True it also clears a stale run lock and marks dead background jobs 'lost'.
        It NEVER releases hardware claims: after an interruption the physical state is unknown."""
        active = self.run_active()
        stale_lock = self.lock_path.exists() and not active
        if apply and stale_lock:
            self.lock_path.unlink(missing_ok=True)
        unfinished = []
        if not active:
            unfinished = [{"id": t["id"], "title": t["title"], "assignee": t["assignee"],
                           "level": "top" if t["parent_id"] is None else "worker"}
                          for t in self.store.list_tickets(status="in_progress")]
        lost, alive_jobs = [], set()
        for j in self.store.list_jobs(state="running"):
            if apply:
                j = self.top.jobs.refresh(j["id"], "recovery")
                live = j["state"] == "running"
            else:
                live = process_alive(j["pid"], j.get("pid_started"))
            if live:
                alive_jobs.add(j["resource"])
            else:
                lost.append({"id": j["id"], "name": j["name"], "resource": j["resource"], "ticket": j["ticket_id"]})
        orphaned = []
        if not active:
            orphaned = [{"name": r["name"], "held_by": r["holder_ticket"], "state": r["state"],
                         "warning": "claimed by work that is no longer running; its physical state is unknown, "
                                    "so verify the hardware before releasing it (release-resource)"}
                        for r in self.store.list_resources() if r["holder_ticket"] and r["name"] not in alive_jobs]
        needed = bool(stale_lock or unfinished or lost or orphaned)
        return {"needed": needed, "run_active": active, "stale_lock": stale_lock, "unfinished_tickets": unfinished,
                "lost_jobs": lost, "orphaned_resources": orphaned, "last_activity": self.store.last_event_ts(),
                "next": ("Tell the human what was interrupted, run `recover`, check any orphaned hardware, then "
                         "`run` to continue; workers restart knowing they were interrupted and check their saved files."
                         if needed else "nothing to recover")}

    def recover(self) -> dict[str, Any]:
        return self.recovery(apply=True)

    def release_resource(self, name: str, reason: str) -> dict[str, Any]:
        """Explicitly free a hardware resource after a human has confirmed its physical state."""
        self.store.force_release_resource(name, "top_manager", reason or "released after recovery")
        return {"released": name}

    def human_said(self, text: str) -> dict[str, Any]:
        """Durably record something the human said or decided (so a restart loses nothing)."""
        append_durable(human_log(self.workspace), "\n**human:** " + text.strip() + "\n")
        return {"recorded": True}

    def handoff(self, ticket_id: str, text: str = "") -> dict[str, Any]:
        """Only for what you (the top manager) could not resolve: put it in front of the human."""
        return actions.handoff(self.store, ticket_id, text, "top_manager")

    def cancel(self, ticket_id: str, text: str = "") -> dict[str, Any]:
        return actions.cancel(self.store, ticket_id, text, "top_manager")

    # ---------------------------------------------------------------- watch

    def _seen(self) -> set[str]:
        try:
            return set(json.loads((self.workspace / "attention_seen.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    def watch(self, timeout: float = 600, grace: float = 10, poll: float = 2.0) -> dict[str, Any]:
        """Block until something needs the human/top manager, then return immediately:
          attention       new escalation or question (reported once)
          pending_no_run  work is waiting but no run is active (start `run`)
          idle            nothing pending and no run active
          timeout         nothing happened within `timeout` seconds
        Run it in the background; the session is notified when it exits."""
        seen = self._seen()
        start = time.monotonic()
        no_run_since: float | None = None
        while True:
            board = self.board()
            new = [a for a in board["needs_attention"] if f"{a['id']}:{a['status']}" not in seen]
            if new:
                seen |= {f"{a['id']}:{a['status']}" for a in new}
                atomic_write(self.workspace / "attention_seen.json", json.dumps(sorted(seen)))
                return {"reason": "attention", "new_attention": new, "run_active": board["run_active"],
                        "still_open": len(board["needs_attention"])}
            pending = any(t["status"] in ("open", "in_progress") for t in self.store.list_tickets())
            if board["run_active"]:
                no_run_since = None
            else:
                no_run_since = no_run_since or time.monotonic()
                if time.monotonic() - no_run_since >= grace:
                    return {"reason": "pending_no_run" if pending else "idle", "run_active": False,
                            "still_open": len(board["needs_attention"]), "halted": board["halted"]}
            if time.monotonic() - start >= timeout:
                return {"reason": "timeout", "run_active": board["run_active"],
                        "still_open": len(board["needs_attention"])}
            time.sleep(poll)

    def state(self, set_text: str | None = None) -> dict[str, Any]:
        if set_text is not None:
            self.top.memory.write_state(set_text)
        return {"state": self.top.memory.read_state()}

    def log(self, text: str) -> dict[str, Any]:
        self.top.memory.append_journal(text)
        return {"logged": True}

    def finish(self, summary: str) -> dict[str, Any]:
        self.top.memory.append_journal(f"FINISHED: {summary}")
        atomic_write(self.workspace / "FINISHED", summary)
        return {"finished": True}

    def pause(self, on: bool) -> dict[str, Any]:
        p = self.workspace / "PAUSE"
        if on:
            p.write_text("paused by top manager", encoding="utf-8")
        elif p.exists():
            p.unlink()
        return {"paused": on}


def main(argv: list[str] | None = None, provider: Provider | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.ctl")
    ap.add_argument("--project", help="project folder (default: $ORCHESTRATOR_PROJECT or the current directory)")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("resume", "board", "run", "pause", "unpause", "recover"):
        sub.add_parser(name)
    sub.add_parser("ticket").add_argument("id")
    c = sub.add_parser("create")
    c.add_argument("--file", help="JSON file (default: read JSON from stdin)")
    p = sub.add_parser("answer")
    p.add_argument("id")
    p.add_argument("text")
    for name in ("retry", "cancel", "handoff"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("text", nargs="?", default="")
    w = sub.add_parser("watch")
    w.add_argument("--timeout", type=float, default=600)
    w.add_argument("--grace", type=float, default=10)
    s = sub.add_parser("state")
    s.add_argument("--set", action="store_true", help="replace state notes with stdin")
    sub.add_parser("log").add_argument("text")
    sub.add_parser("human").add_argument("text")
    rr = sub.add_parser("release-resource")
    rr.add_argument("name")
    rr.add_argument("reason", nargs="?", default="")
    sub.add_parser("finish").add_argument("summary")
    a = ap.parse_args(argv)

    try:
        enter_project(a.project)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    cfg = load_config(a.config)
    ctl = Ctl(cfg, cfg["workspace"], provider)
    try:
        if a.cmd == "create":
            raw = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
            out = ctl.create(json.loads(raw))
        elif a.cmd == "state":
            out = ctl.state(sys.stdin.read() if a.set else None)
        elif a.cmd in ("answer", "retry", "cancel", "handoff"):
            out = getattr(ctl, a.cmd)(a.id, a.text)
        elif a.cmd == "watch":
            out = ctl.watch(a.timeout, a.grace)
        elif a.cmd == "ticket":
            out = ctl.ticket(a.id)
        elif a.cmd == "log":
            out = ctl.log(a.text)
        elif a.cmd == "human":
            out = ctl.human_said(a.text)
        elif a.cmd == "release-resource":
            out = ctl.release_resource(a.name, a.reason)
        elif a.cmd == "finish":
            out = ctl.finish(a.summary)
        elif a.cmd in ("pause", "unpause"):
            out = ctl.pause(a.cmd == "pause")
        else:
            out = getattr(ctl, a.cmd)()
        print(json.dumps(out, indent=2, default=str))
        return 0
    except (StoreError, json.JSONDecodeError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    finally:
        ctl.close()


if __name__ == "__main__":
    sys.exit(main())
