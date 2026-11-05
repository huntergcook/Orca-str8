"""Control interface for an EXTERNAL top manager (e.g. an interactive Claude Code session).

The external agent plays the top manager and talks to the human; this tool is its hands:
it creates tickets, runs the department managers (workers/verifiers run on the configured
API providers), reads results, answers questions and keeps durable notes. All state is in
files and SQLite, so a fresh session can resume with `resume`.

    python -m orchestrator.ctl [--config PATH] <command> [args]      (JSON on stdout)

Commands: resume | board | ticket ID | create [--file F | stdin JSON] | run | watch | answer ID TEXT |
          retry ID [TEXT] | cancel ID [TEXT] | handoff ID [TEXT] | recover | human TEXT | code list|show|accept|reject|discard | models ... | add-resource NAME [NOTE] | safe-to-stop NAME NOTE |
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
from .codework import CodeError, CodeRepo, code_repo_from_cfg, rework_spec
from .config import enter_project, load_config, model_chain
from .hardware import hardware_brief
from .limits import GuardedProvider, Halted
from .memory import human_log
from .modelcfg import DEFAULT_TIERS, current, detect, set_models, use_cli
from .util import append_durable, atomic_write, process_alive, process_stamp
from .providers import CLI_PRESETS, Provider, build_provider, requirements
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
        for m in getattr(self.top, "managers", {}).values():     # let worker threads finish before the store closes
            pool = getattr(m, "pool", None)
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
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
        released = actions.release_abandoned_holds(self.store, "top_manager")
        if released:
            self.store.add_event(None, "top_manager", "note", {"text": f"released abandoned holds: {released}"})
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
        follow_ups = [{"from": t["id"], "title": t["title"][:80], "item": str(f)[:300]}
                      for t in self.store.list_tickets(status="done") if t["parent_id"] is None
                      for f in ((t.get("result") or {}).get("follow_ups") or [])]
        team: dict[str, dict[str, int]] = {}
        for a in self.store.list_agents():
            d = team.setdefault(a["department"] or "?", {"active": 0, "finished": 0})
            d["finished" if a["state"] in ("done", "failed", "escalated") else "active"] += 1
        return {"tickets": rows, "needs_attention": attention,
                "managers_handling": [{k: i[k] for k in ("id", "title", "status", "why")} for i in handled_below],
                "follow_ups": follow_ups, "team": team,
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
                "missing_keys": self.missing_keys(), "missing_clis": self.missing_clis(), "recovery": self.recovery(), **self.where(), **self.board()}

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

    def _models_in_use(self) -> list[str]:
        models: list[str] = []
        for tier in ("manager", "worker"):
            if self.cfg["models"][tier].get("provider") != "external":
                models += model_chain(self.cfg, tier)
        if self.cfg["verification"].get("enabled"):
            models += list(self.cfg["verification"].get("verifier_models") or [])
        return models

    def _missing(self) -> dict[str, list[str]]:
        if self._custom_provider:
            return {"keys": [], "clis": []}
        return requirements(self.cfg, self._models_in_use())

    def missing_keys(self) -> list[str]:
        """API key variables the configured models need but the environment lacks."""
        return self._missing()["keys"]

    def missing_clis(self) -> list[str]:
        """CLI backends (claude, codex, ...) the configured models need but PATH lacks."""
        return self._missing()["clis"]

    def where(self) -> dict[str, Any]:
        ws = self.workspace.resolve()
        info: dict[str, Any] = {"workspace": str(ws), "cwd": str(Path.cwd())}
        if ORCH_HOME in ws.parents:
            info["warning"] = ("this workspace is inside the orchestrator repo itself; run ctl from your "
                               "project folder (where your config/orchestrator.yaml and .env live)")
        return info

    def run(self) -> dict[str, Any]:
        """Run department managers on all open/in-progress tickets. May take a long time."""
        need = self._missing()
        if need["keys"]:
            raise StoreError(f"missing API key(s): {', '.join(need['keys'])}. Put them in {Path.cwd() / '.env'} or the "
                             f"environment. Working dir: {Path.cwd()}; workspace: {self.workspace.resolve()}")
        if need["clis"]:
            raise StoreError(f"required CLI(s) not found on PATH: {', '.join(need['clis'])}. Install and log in, or "
                             "choose other models with `ctl models`.")
        if self.run_active():
            raise StoreError(f"a run is already active (pid {self.lock_path.read_text().strip()}); "
                             "watch `board` instead of starting another")
        atomic_write(self.lock_path, process_stamp())      # pid + start time: stays correct across reboots
        try:
            return self._run()
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _run(self) -> dict[str, Any]:
        self.store.stop_agents()          # live status shows only this run's agents, not leftovers of an interrupted one
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

    def balance(self) -> dict[str, Any]:
        """Real account balances (providers that report one, e.g. DeepSeek) next to the orchestrator's estimate."""
        from .limits import cost_estimate
        from .providers import fetch_balances
        b = self.cfg["budget"]
        return {"balances": fetch_balances(self.cfg), "estimated_spend_usd": cost_estimate(self.store, self.cfg),
                "max_cost_usd": b.get("max_cost_usd"), "stop_below_balance_usd": b.get("stop_below_balance_usd"),
                "note": "the estimate uses the config's price table; the balance is what the provider reports"}

    def _cancelled_by_top(self, t: dict[str, Any]) -> bool:
        if t["status"] != "cancelled":
            return False
        for e in reversed(self.store.events(t["id"])):
            if e["type"] == "status_changed" and e["payload"].get("to") == "cancelled":
                return e["actor"] == "top_manager"
        return False

    def set_models(self, ticket_id: str, manager: str | None = None, worker: str | None = None) -> dict[str, Any]:
        """The top manager picks models per ticket (it applies to everything below it): e.g. the cheap model by
        default and a stronger one for a hard or repeatedly failing ticket. 'default' removes a choice."""
        known = set(self._models_in_use()) | set(self.cfg.get("prices") or {})
        for m in (manager, worker):
            if m and m != "default" and m not in known:
                raise StoreError(f"unknown model {m!r}; configured models: {sorted(known)}")
        changes = {k: (None if v == "default" else v) for k, v in (("manager", manager), ("worker", worker)) if v}
        if not changes:
            raise StoreError("give --manager and/or --worker (a model name, or 'default' to use the config)")
        t = self.store.set_models(ticket_id, "top_manager", changes)
        return {"ticket_id": ticket_id, "models": t.get("models") or {}}

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

    def add_resource(self, name: str, safe_to_stop: str = "") -> dict[str, Any]:
        """Declare a piece of hardware so workers can claim it (the Experiment department's resource table)."""
        self.store.register_resource(name, safe_to_stop)
        return {"resources": [r["name"] for r in self.store.list_resources()]}

    def set_safe_to_stop(self, name: str, note: str) -> dict[str, Any]:
        self.store.set_safe_to_stop(name, note, actor="top_manager")
        return {"resource": name, "safe_to_stop": note}

    def release_resource(self, name: str, reason: str) -> dict[str, Any]:
        """Explicitly free a hardware resource after a human has confirmed its physical state."""
        self.store.force_release_resource(name, "top_manager", reason or "released after recovery")
        return {"released": name}

    def human_said(self, text: str) -> dict[str, Any]:
        """Durably record something the human said or decided (so a restart loses nothing)."""
        append_durable(human_log(self.workspace), "\n**human:** " + text.strip() + "\n")
        return {"recorded": True}

    # ------------------------------------------------------------- code review

    def _repo(self) -> CodeRepo:
        repo = code_repo_from_cfg(self.cfg)
        if repo is None:
            raise StoreError("no git repository for code tickets (set code.repo in the config, or run inside a git repo)")
        return repo

    def _code_state(self, ticket_id: str) -> str | None:
        """Where a code ticket's branch stands: 'ready' (awaiting review), 'accepted', 'rejected', 'discarded'."""
        last = [e["type"] for e in self.store.events(ticket_id)
                if e["type"] in ("code_ready", "code_accepted", "code_rejected", "code_discarded")]
        return last[-1].removeprefix("code_") if last else None

    def code_list(self) -> dict[str, Any]:
        rows = []
        for t in self.store.list_tickets(status="done"):
            if t.get("code") is not None and self._code_state(t["id"]) == "ready":
                res = (t.get("result") or {})
                gates = [e for e in self.store.events(t["id"]) if e["type"] == "code_gates"]
                rows.append({"id": t["id"], "title": t["title"], "files": (res.get("code") or {}).get("files", []),
                             "gates_passed": bool(gates and gates[-1]["payload"]["ok"]),
                             "summary": str(res.get("summary", ""))[:300]})
        repo = code_repo_from_cfg(self.cfg)
        return {"awaiting_review": rows, "integration": repo.integration_status() if repo else None,
                "next": "ctl code show ID, then ctl code accept ID (into integration) or ctl code reject ID \"feedback\""
                        if rows else "nothing awaiting review"}

    def _ready_ticket(self, ticket_id: str) -> dict[str, Any]:
        t = self.store.get_ticket(ticket_id)
        state = self._code_state(ticket_id)
        if t.get("code") is None or state != "ready":
            raise StoreError(f"{ticket_id} is not awaiting code review (state: {state or 'not a code ticket'})")
        return t

    def code_show(self, ticket_id: str, max_lines: int = 300) -> dict[str, Any]:
        """The review packet: what changed, gate results, the worker's own summary and open questions, the diff."""
        t, repo = self._ready_ticket(ticket_id), self._repo()
        res = t.get("result") or {}
        gates = [e for e in self.store.events(ticket_id) if e["type"] == "code_gates"]
        return {"id": ticket_id, "title": t["title"], "summary": res.get("summary"), "open_questions": res.get("open_questions"),
                "files": [f"{s} {p}" for s, p in repo.changed_files(ticket_id)], "diffstat": repo.diffstat(ticket_id),
                "gates": gates[-1]["payload"]["gates"] if gates else [], "diff": repo.diff(ticket_id, max_lines)}

    def code_accept(self, ticket_id: str) -> dict[str, Any]:
        t, repo = self._ready_ticket(ticket_id), self._repo()
        code_cfg = self.cfg.get("code") or {}
        out = repo.merge_to_integration(ticket_id, f"Merge {ticket_id}: {t['title']}",
                                        list(code_cfg.get("integration_checks") or []), float(code_cfg.get("test_timeout", 600)))
        self.store.add_event(ticket_id, "top_manager", "code_accepted",
                             {"merge_commit": out["merge_commit"], "by": "top_manager", "into": repo.integration_branch})
        return {"accepted": ticket_id, "merge_commit": out["merge_commit"], "into": repo.integration_branch,
                "next": "ctl code integration shows what is waiting; ctl code release merges it into " + repo.base_branch}

    def code_integration(self) -> dict[str, Any]:
        """What the integration branch holds beyond the base branch (the managers' accepted work)."""
        return self._repo().integration_status()

    def code_release(self) -> dict[str, Any]:
        """Merge the integration branch into the base branch (the repository must be on it, with no uncommitted
        changes to tracked files)."""
        repo = self._repo()
        ccfg = self.cfg.get("code") or {}
        out = repo.release(f"Release integration ({repo.integration_status()['ahead']} commits) into {repo.base_branch}",
                           checks=list(ccfg.get("integration_checks") or []),
                           timeout=float(ccfg.get("test_timeout", 600)))
        self.store.add_event(None, "top_manager", "code_released", out)
        return out

    def code_sync(self) -> dict[str, Any]:
        """Bring commits made on the base branch into the integration branch."""
        return {"merged_base_into_integration": self._repo().sync_integration()}

    def code_reject(self, ticket_id: str, feedback: str) -> dict[str, Any]:
        """Discard the branch and file a replacement ticket that carries the reviewer's feedback."""
        if not feedback.strip():
            raise StoreError("reject needs feedback: say what is wrong and what right looks like")
        t, repo = self._ready_ticket(ticket_id), self._repo()
        root = t
        while root.get("parent_id"):
            root = self.store.get_ticket(root["parent_id"])
        worker = rework_spec(t, feedback)
        new = self.store.create_ticket({
            "title": f"Redo: {t['title']}", "assignee": root["assignee"], "worker_role": "worker", "cc": ["top_manager"],
            "context": worker["context"], "acceptance_criteria": t["acceptance_criteria"], "return_spec": t["return_spec"],
            "rigor": worker["rigor"], "worker_tickets": [worker]}, actor="top_manager")
        repo.discard(ticket_id)
        self.store.add_event(ticket_id, "top_manager", "code_rejected", {"feedback": feedback.strip()[:500], "replacement": new["id"]})
        return {"rejected": ticket_id, "replacement_ticket": new["id"], "assignee": new["assignee"]}

    def code_discard(self, ticket_id: str) -> dict[str, Any]:
        self._ready_ticket(ticket_id)
        self._repo().discard(ticket_id)
        self.store.add_event(ticket_id, "top_manager", "code_discarded", {})
        return {"discarded": ticket_id}

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
          completed       a department finished a top-level ticket: read its result and follow-ups, assign next work
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
            finished = [t for t in self.store.list_tickets() if t["parent_id"] is None
                        and t["status"] in ("done", "failed", "cancelled") and f"{t['id']}:{t['status']}" not in seen]
            if finished:
                seen |= {f"{t['id']}:{t['status']}" for t in finished}
                atomic_write(self.workspace / "attention_seen.json", json.dumps(sorted(seen)))
                finished = [t for t in finished if not self._cancelled_by_top(t)]   # your own cancels are not news
            if finished:
                return {"reason": "completed", "run_active": board["run_active"],
                        "completed": [{"id": t["id"], "title": t["title"], "status": t["status"],
                                       "assignee": t["assignee"], "result": t.get("result"),
                                       "follow_ups": (t.get("result") or {}).get("follow_ups") or []} for t in finished],
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


def _models_command(a: argparse.Namespace, cfg: dict[str, Any]) -> int:
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    try:
        if a.action == "detect":
            out: dict[str, Any] = detect()
        elif a.action == "show":
            out = {"current": current(cfg), "detected": detect()}
        elif a.action == "use-cli":
            if not a.cli:
                raise ValueError("use-cli needs --cli (claude or codex)")
            out = use_cli(a.config, a.cli, a.model, tiers)
        else:
            if not a.provider:
                raise ValueError("set needs --provider (and usually --model)")
            out = set_models(a.config, tiers, a.provider, a.model, a.fallback, a.clear_fallback)
        print(json.dumps(out, indent=2, default=str))
        return 0
    except (ValueError, OSError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


def main(argv: list[str] | None = None, provider: Provider | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.ctl")
    ap.add_argument("--project", help="project folder (default: $ORCHESTRATOR_PROJECT or the current directory)")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("resume", "board", "run", "pause", "unpause", "recover", "balance"):
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
    cd = sub.add_parser("code", help="review the code branches workers produced")
    cd.add_argument("action", choices=["list", "show", "accept", "reject", "discard", "integration", "release", "sync"])
    cd.add_argument("id", nargs="?")
    cd.add_argument("text", nargs="?", default="", help="feedback for reject")
    cd.add_argument("--lines", type=int, default=300, help="diff lines to show")
    w = sub.add_parser("watch")
    w.add_argument("--timeout", type=float, default=600)
    w.add_argument("--grace", type=float, default=10)
    s = sub.add_parser("state")
    s.add_argument("--set", action="store_true", help="replace state notes with stdin")
    sub.add_parser("log").add_argument("text")
    sub.add_parser("human").add_argument("text")
    m = sub.add_parser("models", help="show/detect/set which models run each role")
    m.add_argument("action", nargs="?", default="show", choices=["show", "detect", "set", "use-cli"])
    m.add_argument("--tiers", default=",".join(DEFAULT_TIERS), help="comma list of: manager, worker, verifier, top")
    m.add_argument("--provider")
    m.add_argument("--model", default="default")
    m.add_argument("--cli", choices=sorted(CLI_PRESETS))
    m.add_argument("--fallback", action="append", help="provider:model (repeatable)")
    m.add_argument("--clear-fallback", action="store_true")
    ar = sub.add_parser("add-resource")
    ar.add_argument("name")
    ar.add_argument("safe_to_stop", nargs="?", default="")
    ss = sub.add_parser("safe-to-stop")
    ss.add_argument("name")
    ss.add_argument("note")
    rr = sub.add_parser("release-resource")
    rr.add_argument("name")
    rr.add_argument("reason", nargs="?", default="")
    sm = sub.add_parser("set-models", help="choose models for one ticket and everything below it")
    sm.add_argument("id")
    sm.add_argument("--manager", help="model name, or 'default'")
    sm.add_argument("--worker", help="model name, or 'default'")
    sub.add_parser("finish").add_argument("summary")
    a = ap.parse_args(argv)

    try:
        enter_project(a.project)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    cfg = load_config(a.config)
    if a.cmd == "models":
        return _models_command(a, cfg)
    ctl = Ctl(cfg, cfg["workspace"], provider)
    try:
        if a.cmd == "create":
            raw = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
            out = ctl.create(json.loads(raw))
        elif a.cmd == "state":
            out = ctl.state(sys.stdin.read() if a.set else None)
        elif a.cmd in ("answer", "retry", "cancel", "handoff"):
            out = getattr(ctl, a.cmd)(a.id, a.text)
        elif a.cmd == "code":
            if a.action not in ("list", "integration", "release", "sync") and not a.id:
                raise StoreError(f"code {a.action} needs a ticket id")
            out = {"list": lambda: ctl.code_list(), "show": lambda: ctl.code_show(a.id, a.lines),
                   "accept": lambda: ctl.code_accept(a.id), "reject": lambda: ctl.code_reject(a.id, a.text),
                   "discard": lambda: ctl.code_discard(a.id), "integration": lambda: ctl.code_integration(),
                   "release": lambda: ctl.code_release(), "sync": lambda: ctl.code_sync()}[a.action]()
        elif a.cmd == "watch":
            out = ctl.watch(a.timeout, a.grace)
        elif a.cmd == "ticket":
            out = ctl.ticket(a.id)
        elif a.cmd == "log":
            out = ctl.log(a.text)
        elif a.cmd == "human":
            out = ctl.human_said(a.text)
        elif a.cmd == "add-resource":
            out = ctl.add_resource(a.name, a.safe_to_stop)
        elif a.cmd == "safe-to-stop":
            out = ctl.set_safe_to_stop(a.name, a.note)
        elif a.cmd == "release-resource":
            out = ctl.release_resource(a.name, a.reason)
        elif a.cmd == "set-models":
            out = ctl.set_models(a.id, a.manager, a.worker)
        elif a.cmd == "finish":
            out = ctl.finish(a.summary)
        elif a.cmd in ("pause", "unpause"):
            out = ctl.pause(a.cmd == "pause")
        else:
            out = getattr(ctl, a.cmd)()
        print(json.dumps(out, indent=2, default=str))
        return 0
    except (StoreError, CodeError, json.JSONDecodeError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    finally:
        ctl.close()


if __name__ == "__main__":
    sys.exit(main())
