"""The daemon: persistence lives here, not in anyone's chat session.

You set a goal, walk away, and come back when it is met. The daemon owns the loop that makes that true:

  * it runs the departments until nothing is runnable, wakes the director (a short, stateless model call whose context
    is rebuilt from the mission brief + notes + board) whenever something needs a decision, and repeats;
  * it never exits just because the board is empty: it ends only when the goal's checks pass (or the human says stop);
  * quota and usage limits are waited out, budgets and pauses park the run (and tell you), crashes are restarted;
  * work that needs a human is announced through `notify`, and everything else continues on logged assumptions.

    python -m orchestrator.daemon --project DIR run         the loop, in the foreground
    python -m orchestrator.daemon --project DIR supervise   the loop, restarted whenever it dies (use this)
    python -m orchestrator.daemon --project DIR start|stop|status|install [--print]

Interactive Claude Code / Codex sessions are optional clients: closing them changes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import actions, mission, providers, winsys
from .config import enter_project, load_config, model_chain
from .ctl import Ctl
from .limits import Halted
from .notify import Notifier
from .store import StoreError
from .util import append_durable, atomic_write, process_alive, process_stamp, parse_stamp

FINAL_TITLE = "Final verification against the mission brief"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_checks(commands: list[str], cwd: str | Path, timeout: float = 600.0) -> list[dict[str, Any]]:
    """Run the mission's success checks (shell commands that exit 0 when a criterion is met)."""
    out = []
    for cmd in commands:
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, errors="replace",
                               timeout=timeout)
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-600:]
            out.append({"command": cmd, "ok": r.returncode == 0, "rc": r.returncode, "tail": tail})
        except subprocess.TimeoutExpired:
            out.append({"command": cmd, "ok": False, "rc": None, "tail": f"timed out after {int(timeout)}s"})
        except OSError as e:
            out.append({"command": cmd, "ok": False, "rc": None, "tail": f"{type(e).__name__}: {e}"})
    return out


def _acquire(path: Path) -> bool:
    """Take a pid+start-time lock file. False if a live process holds it (a stale one is taken over)."""
    try:
        pid, started = parse_stamp(path.read_text())
        if process_alive(pid, started) and pid != os.getpid():
            return False
    except OSError:
        pass
    atomic_write(path, process_stamp())
    return True


class Daemon:
    def __init__(self, project: str | Path, cfg: dict[str, Any] | None = None, provider: Any = None,
                 sleep: Callable[[float], None] = time.sleep, notifier: Notifier | None = None) -> None:
        self.project = Path(project).resolve()
        self._cfg_override = cfg
        self.provider = provider                       # tests inject a scripted provider
        self.sleep = sleep
        self._notifier = notifier
        self.config_path = self.project / "config" / "orchestrator.yaml"
        cfg0 = self._load_cfg()
        self.workspace = Path(cfg0["workspace"]) if Path(cfg0["workspace"]).is_absolute() else self.project / cfg0["workspace"]
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workspace / "daemon_state.json"
        self.state: dict[str, Any] = self._read_state()
        self.stop_file, self.log_path = self.workspace / "STOP", self.workspace / "daemon.log"
        self._unavail_since: float | None = None
        self._announced: set[str] = set()
        self._first_tick = True
        self._director_fail = 0

    # ------------------------------------------------------------------ plumbing

    def _load_cfg(self) -> dict[str, Any]:
        if self._cfg_override is not None:
            return self._cfg_override
        return load_config(self.config_path if self.config_path.exists() else None)

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **kw: Any) -> None:
        self.state.update(kw)
        self.state["updated"] = _now()
        atomic_write(self.state_path, json.dumps(self.state, indent=1, default=str))

    def log(self, text: str) -> None:
        try:
            append_durable(self.log_path, f"{_now()} {text}\n")
        except OSError:
            pass

    def notifier(self, cfg: dict[str, Any]) -> Notifier:
        return self._notifier or Notifier(cfg, self.workspace)

    def _once(self, key: str) -> bool:
        """True the first time `key` is seen (per daemon process and per state file), so alerts are not repeated."""
        seen = set(self.state.get("announced", []))
        if key in seen or key in self._announced:
            return False
        self._announced.add(key)
        self.state["announced"] = sorted(seen | {key})[-300:]
        return True

    # ------------------------------------------------------------------ completion

    def _checks(self, ctl: Ctl) -> tuple[bool, str, list[dict[str, Any]]]:
        cmds = mission.success_checks(ctl.cfg)
        if not cmds:
            return True, "", []
        res = run_checks(cmds, self.project, float(ctl.cfg["code"].get("test_timeout", 600)))
        failed = [r for r in res if not r["ok"]]
        report = "\n".join(f"- {'PASS' if r['ok'] else 'FAIL'} `{r['command']}`" + ("" if r["ok"] else f" (rc {r['rc']}): {r['tail']}")
                           for r in res)
        try:                                              # keep the proof: what ran, what it printed
            ev = self.workspace / "evidence"
            ev.mkdir(exist_ok=True)
            atomic_write(ev / f"checks-{time.strftime('%Y%m%d-%H%M%S')}.txt",
                         "\n\n".join(f"$ {r['command']}\nexit {r['rc']}\n{r.get('tail', '')}" for r in res))
        except Exception:
            pass
        return not failed, report, res

    def finish_gate(self, ctl: Ctl) -> tuple[bool, str]:
        """May the project end? Machine checks decide when the mission has them; otherwise an independent final
        verification ticket must pass (when the Verification department exists)."""
        ok, report, res = self._checks(ctl)
        if res:
            return ok, ("all success checks pass" if ok else f"success checks failing:\n{report}")
        if "verification" not in ctl.cfg.get("departments", {}):
            return True, "no machine checks and no verification department: the director's judgement stands"
        tickets = [t for t in ctl.store.list_tickets() if t["title"] == FINAL_TITLE and t["parent_id"] is None]
        done = [t for t in tickets if t["status"] == "done"]
        if done and str((done[-1].get("result") or {}).get("verdict", "")).lower() == "pass":
            return True, "final verification passed"
        if not any(t["status"] in ("open", "in_progress", "needs_clarification", "escalated", "blocked") for t in tickets):
            ctl.director.create_ticket({
                "department": "verification", "title": FINAL_TITLE, "rigor": "rigorous",
                "context": ("Act as a hostile reviewer. Check the project's results against the mission brief and each success "
                            "criterion with real evidence from the workspace: run it, open the files, read the outputs. Try to "
                            "break the result (edge cases, a clean checkout, the not_goals and hard_constraints). Do not accept "
                            "claims or green checks at face value: ask what a passing check would miss. Save evidence under "
                            "workspace/evidence/. Verdict pass only if every criterion is proven."),
                "acceptance_criteria": ["every success criterion in the mission brief is checked against evidence"],
                "return_spec": {"format": "json", "description": '{"verdict": "pass|concerns|fail", "gaps": ["..."]}'},
                "worker_role": "verifier"})
            return False, "final verification requested; it must pass before the project can finish"
        return False, "final verification has not passed yet"

    # ------------------------------------------------------------------ one cycle

    def _top_level(self, ctl: Ctl) -> list[dict[str, Any]]:
        return [t for t in ctl.store.list_tickets() if t["parent_id"] is None and t["title"] != FINAL_TITLE]

    def _progress_signature(self, ctl: Ctl) -> str:
        tickets = ctl.store.list_tickets()
        done = sum(1 for t in tickets if t["status"] == "done")
        return f"{done}/{len(tickets)}"

    def _director_turn(self, ctl: Ctl, kind: str, text: str) -> bool:
        """One director decision. Failures are counted and backed off; they never take the daemon down."""
        ctl.director.finish_gate = lambda: self.finish_gate(ctl)
        ctl.director.notify = self.notifier(ctl.cfg)
        if self.provider is None and ctl.cfg["models"]["director"].get("provider") == "external":
            ctl.director.models = model_chain(ctl.cfg, "manager")     # nobody is attached to play it: use the manager models
        try:
            ctl.director.event_turn(kind, text)
            self._director_fail = 0
            return True
        except Halted:
            raise
        except Exception as e:
            self._director_fail += 1
            self.log(f"director turn failed ({kind}): {type(e).__name__}: {e}")
            if self._director_fail >= 3:
                self.notifier(ctl.cfg)("error", "The director keeps failing",
                                       f"{type(e).__name__}: {str(e)[:300]} (will keep retrying with backoff)",
                                       key="director_fail", min_gap=3600)
            return False

    def tick(self) -> str:
        """One cycle. Returns what the daemon is doing: finished | waiting_mission | paused | parked | waiting_quota |
        working | idle | stopped."""
        if self.stop_file.exists():
            return "stopped"
        cfg = self._load_cfg()
        notify = self.notifier(cfg)
        pol = cfg["policies"]
        if (self.workspace / "FINISHED").exists():
            return "finished"

        # 1. the mission brief must exist and be complete before any work starts
        mp = mission.path_for(cfg)
        if not mp.exists():
            mission.scaffold(mp)
            if self._once("mission_scaffolded"):
                notify("blocked", "Fill in the mission brief", f"Created {mp}. Answer the required questions (or start your "
                       "agent and let the director interview you); the daemon waits until they are answered.")
        gate = mission.gate_error(cfg)
        if gate:
            if self._once(f"mission_incomplete:{mission.charter_hash(cfg)}"):
                notify("blocked", "Mission brief incomplete", gate)
            self._save(phase="waiting_mission")
            return "waiting_mission"

        # 2. pause switch
        if (self.workspace / "PAUSE").exists():
            self._save(phase="paused")
            return "paused"

        # 3. models that hit a usage limit are re-tried after a wait; until then the run waits instead of failing
        if providers.UNAVAILABLE:
            self._unavail_since = self._unavail_since or time.monotonic()
            waited = time.monotonic() - self._unavail_since
            if waited >= 60 * float(pol.get("model_retry_minutes", 30)):
                providers.UNAVAILABLE.clear()
                self._unavail_since = None
            else:
                mgr = model_chain(cfg, "manager")
                if not providers.usable(mgr):
                    notify("blocked", "Waiting for model quota", "; ".join(f"{m}: {w}" for m, w in providers.UNAVAILABLE.items()),
                           key="quota", min_gap=6 * 3600)
                    self._save(phase="waiting_quota")
                    return "waiting_quota"
        else:
            self._unavail_since = None

        ctl = Ctl(cfg, self.workspace, self.provider)
        try:
            if self._first_tick:
                self._first_tick = False
                rec = ctl.recover()
                if rec.get("needed"):
                    self.log(f"recovered after interruption: {json.dumps(rec, default=str)[:400]}")
                    notify("info", "Daemon restarted and recovered", json.dumps({k: rec[k] for k in (
                        "unfinished_tickets", "lost_jobs", "orphaned_resources") if rec.get(k)}, default=str)[:600])
            return self._cycle(ctl, cfg, notify)
        finally:
            ctl.close()

    def _cycle(self, ctl: Ctl, cfg: dict[str, Any], notify: Notifier) -> str:
        pol = cfg["policies"]
        # a. run the departments until nothing is runnable
        try:
            out = ctl.run()
        except StoreError as e:                         # e.g. a human started `ctl run`, or a CLI / key is missing
            self.log(f"run refused: {e}")
            if "already active" not in str(e):
                notify("blocked", "The harness cannot run", str(e)[:500], key="run_refused", min_gap=6 * 3600)
            self._save(phase="idle")
            return "idle"
        halted = out.get("halted")
        if halted:
            if "PAUSE" in halted.upper() or "paused" in halted.lower():
                self._save(phase="paused")
                return "paused"
            notify("blocked", "Run parked: a limit was reached", f"{halted}. Raise the limit in the config or add funds; the daemon "
                   "resumes by itself.", key=f"halt:{halted[:60]}", min_gap=6 * 3600)
            self._save(phase="parked", halted=halted)
            return "parked"
        self._save(halted=None)

        board = ctl.board()
        attention = board.get("needs_attention", [])

        # b. anything for the human goes out as a notification (work elsewhere continues)
        for item in attention:
            if item.get("route") == "human":
                if self._once(f"human:{item['id']}:{item.get('status')}"):
                    notify("blocked", f"Needs you: {item.get('title', item['id'])[:80]}",
                           f"{item.get('why') or item.get('open_question') or ''}\nticket {item['id']}")
        mine = [i for i in attention if i.get("route") == "director"]

        # c. progress watchdog: no movement anywhere for stall_hours -> the director must change strategy
        sig = self._progress_signature(ctl)
        now = time.time()
        if sig != self.state.get("progress_sig"):
            self._save(progress_sig=sig, progress_at=now, stall_flagged=False)
        elif now - float(self.state.get("progress_at", now)) > 3600 * float(pol.get("stall_hours", 4)) \
                and not self.state.get("stall_flagged") and not mine:
            self._save(stall_flagged=True)
            hrs = pol.get("stall_hours", 4)
            notify("info", "No progress", f"Nothing has completed for {hrs} h; the director is changing strategy.")
            self._director_turn(ctl, "no_progress", f"Nothing has completed anywhere for {hrs} hours. Review the board and your "
                                "notes, say what is blocking the goal, and change strategy: split the work, try another approach "
                                "or model, research alternatives, or ask the human only if truly necessary.")
            return "working"

        # d. tickets escalated to the director: it decides (answer / retry / cancel / assume), a few tries, then the human
        if mine:
            lines = "\n".join(f"- {i['id']} [{i.get('status')}] {i.get('title', '')[:80]}: "
                              f"{(i.get('why') or i.get('open_question') or '')[:300]}" for i in mine)
            tries = self.state.setdefault("director_tries", {})
            for i in mine:
                tries[i["id"]] = tries.get(i["id"], 0) + 1
            self._director_turn(ctl, "attention", f"These tickets are waiting on you:\n{lines}\n"
                                "Resolve each: answer the question, retry with guidance (another model or approach), cancel and "
                                "re-plan, or make and log an assumption. Do not wait for the human unless the brief requires it.")
            for i in mine:
                if tries.get(i["id"], 0) >= 3:
                    try:
                        actions.handoff(ctl.store, i["id"], "the director could not resolve this in 3 attempts", "director")
                    except StoreError:
                        pass
            self._save(director_tries=tries)
            return "working"

        # e. finished top-level tickets: the director reads results and follow-ups and plans what is next
        seen = set(self.state.get("seen_done", []))
        fresh = [t for t in self._top_level(ctl) if t["status"] in ("done", "failed", "cancelled")
                 and f"{t['id']}:{t['status']}" not in seen]
        open_work = [t for t in self._top_level(ctl) if t["status"] in ("open", "in_progress", "needs_clarification", "blocked", "escalated")]
        if fresh:
            body = "\n".join(f"- {t['id']} [{t['status']}] {t['title'][:80]} -> {json.dumps(t.get('result'), default=str)[:800]}"
                             for t in fresh)
            ok = self._director_turn(ctl, "completed", f"Departments finished:\n{body}\nRead results and follow_ups; create the "
                                     "next tickets toward the mission's goal, or finish if the success criteria are met.")
            if ok:
                self._save(seen_done=sorted(seen | {f"{t['id']}:{t['status']}" for t in fresh})[-500:])
            if (self.workspace / "FINISHED").exists():
                return "finished"
            return "working"

        if open_work:
            self._save(phase="idle")
            return "idle"                                # waiting on a human, or on a ticket that is backing off

        # f. nothing open and nothing new: is the goal actually met? never just stop because the board is empty
        tops = self._top_level(ctl)
        checks = mission.success_checks(cfg)
        sig = self._progress_signature(ctl)
        if tops and checks:
            ok, report, _ = self._checks(ctl)
            if ok:
                ctl.finish("All of the mission's success checks pass.")
                try:
                    ctl.report()
                except Exception:
                    pass
                notify("done", "Goal met", f"All success checks pass:\n{report}"[:1500])
                self._save(phase="finished")
                return "finished"
            why = f"success checks failing:\n{report}"
        elif tops:
            why = ("there are no machine-checkable criteria; judge the success criteria against the evidence and use `finish` "
                   "if they are met (an independent final verification may be required), otherwise create tickets for the gaps")
        else:
            why = ""
        gap = self.state.get("gap_turns", 0) if self.state.get("last_gap_sig") == sig else 0
        if gap >= 3:
            # the director was asked repeatedly and produced no new work: stop burning tokens, tell the human, wait
            if self._once(f"gap_stuck:{sig}"):
                notify("blocked", "Stuck: goal unmet and no next step", f"{why}\nThe director produced no new work in {gap} tries.")
            self._save(phase="stuck")
            return "idle"
        self._save(last_gap_sig=sig, gap_turns=gap + 1)
        if not tops:
            self._director_turn(ctl, "start", "The project has no tickets yet. Read the mission brief and create the first tickets "
                                "for the departments (research first when the path is unclear). Ask the human only what the "
                                "brief leaves open.")
        else:
            self._director_turn(ctl, "goal_unmet", f"All tickets are finished but the goal is not verifiably met: {why}\n"
                                "Do a gap analysis against the success criteria, then create tickets that close the gaps. If the "
                                "remaining gap needs something only the human can provide, use ask_human with a precise question.")
        if (self.workspace / "FINISHED").exists():
            return "finished"
        self._save(phase="working")
        return "working"

    # ------------------------------------------------------------------ loops

    def run(self, max_ticks: int | None = None) -> str:
        """Loop until finished or stopped. Any exception inside a cycle is logged and retried with backoff."""
        if not _acquire(self.workspace / "DAEMON_RUN"):
            self.log("another daemon is already running for this project")
            return "already_running"
        winsys.keep_awake()
        self._save(started=_now(), pid=os.getpid(), gap_turns=0)      # a (re)start gives the director fresh tries
        crashes, ticks = 0, 0
        try:
            while max_ticks is None or ticks < max_ticks:
                ticks += 1
                try:
                    phase = self.tick()
                    crashes = 0
                except Halted:
                    phase = "parked"
                except Exception as e:
                    crashes += 1
                    phase = "error"
                    self.log(f"cycle crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
                    try:
                        self.notifier(self._load_cfg())("error", "Daemon cycle crashed (retrying)",
                                                        f"{type(e).__name__}: {str(e)[:300]}", key="crash", min_gap=1800)
                    except Exception:
                        pass
                self.state["phase"] = phase
                self._save()
                if phase in ("finished", "stopped"):
                    return phase
                poll = float(self._load_cfg()["policies"].get("daemon_poll_seconds", 10))
                if phase == "error":
                    self.sleep(min(poll * (2 ** min(crashes, 6)), 600))
                elif phase in ("idle", "waiting_mission", "paused", "parked", "waiting_quota"):
                    self.sleep(poll if phase != "waiting_quota" else max(poll, 60))
            return "max_ticks"
        finally:
            winsys.allow_sleep()
            try:
                (self.workspace / "DAEMON_RUN").unlink(missing_ok=True)
            except OSError:
                pass

    def supervise(self, max_restarts: int | None = None) -> int:
        """Restart the daemon whenever it exits without the project being finished (or stopped by the human)."""
        if not _acquire(self.workspace / "DAEMON"):
            print("a daemon supervisor is already running for this project", file=sys.stderr)
            return 3
        winsys.keep_awake()
        winsys.kill_children_on_exit()
        backoff, restarts = 5.0, 0
        try:
            while True:
                if (self.workspace / "FINISHED").exists():
                    return 0
                if self.stop_file.exists():
                    return 0
                started = time.monotonic()
                self.log("supervisor: starting daemon")
                proc = subprocess.Popen([sys.executable, "-P", "-m", "orchestrator.daemon", "--project", str(self.project), "run"],
                                        cwd=str(self.project))
                rc = proc.wait()
                if (self.workspace / "FINISHED").exists() or self.stop_file.exists():
                    return 0
                ran = time.monotonic() - started
                backoff = 5.0 if ran > 600 else min(backoff * 2, 300.0)
                restarts += 1
                self.log(f"supervisor: daemon exited with {rc} after {int(ran)}s; restarting in {int(backoff)}s")
                if rc == 3:                                  # a second daemon: leave the first one alone
                    return 3
                if restarts % 5 == 0:
                    self.notifier(self._load_cfg())("error", "Daemon keeps exiting", f"{restarts} restarts; last exit code {rc}",
                                                    key="restarts", min_gap=3600)
                if max_restarts is not None and restarts >= max_restarts:
                    return 1
                time.sleep(backoff)
        finally:
            try:
                (self.workspace / "DAEMON").unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        st = self._read_state()
        sup_pid, sup_started = (None, None)
        run_pid, run_started = (None, None)
        try:
            sup_pid, sup_started = parse_stamp((self.workspace / "DAEMON").read_text())
        except OSError:
            pass
        try:
            run_pid, run_started = parse_stamp((self.workspace / "DAEMON_RUN").read_text())
        except OSError:
            pass
        cfg = self._load_cfg()
        out = {"project": str(self.project), "phase": st.get("phase", "not started"), "updated": st.get("updated"),
               "supervisor_alive": process_alive(sup_pid, sup_started), "daemon_alive": process_alive(run_pid, run_started),
               "finished": (self.workspace / "FINISHED").exists(), "paused": (self.workspace / "PAUSE").exists(),
               "mission": {k: v for k, v in mission.status(cfg).items() if k in ("exists", "complete", "unanswered_required")},
               "halted": st.get("halted"), "models_unavailable": dict(providers.UNAVAILABLE)}
        db = self.workspace / "orchestrator.db"
        if db.exists():
            from .store import Store
            s = Store(db)
            try:
                tickets = [t for t in s.list_tickets() if t["parent_id"] is None]
                out["tickets"] = {k: sum(1 for t in tickets if t["status"] == k) for k in sorted({t["status"] for t in tickets})}
            finally:
                s.close()
        return out


def _spawn_detached(project: Path) -> None:
    ws = project / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    log = open(ws / "supervisor.log", "ab")
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        flags["start_new_session"] = True
    subprocess.Popen(winsys.service_command(project), cwd=str(project), stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, **flags)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.daemon")
    ap.add_argument("--project", default=None, help="project folder (default: $ORCHESTRATOR_PROJECT or the current folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "supervise", "start", "stop", "status"):
        sub.add_parser(name)
    inst = sub.add_parser("install", help="register the supervisor to start at logon (Windows Task Scheduler / systemd unit)")
    inst.add_argument("--print", action="store_true", dest="dry", help="only show what would be installed")
    a = ap.parse_args(argv)
    project = enter_project(a.project) or Path.cwd()
    d = Daemon(project)
    if a.cmd == "run":
        r = d.run()
        return 0 if r in ("finished", "stopped") else 3 if r == "already_running" else 1
    if a.cmd == "supervise":
        return d.supervise()
    if a.cmd == "start":
        (d.workspace / "STOP").unlink(missing_ok=True)
        _spawn_detached(project)
        print(json.dumps({"started": True, "log": str(d.workspace / "supervisor.log")}))
        return 0
    if a.cmd == "stop":
        atomic_write(d.workspace / "STOP", "stop requested")
        print(json.dumps({"stop_requested": True, "note": "the daemon exits at its next cycle; delete workspace/STOP to allow starting again"}))
        return 0
    if a.cmd == "status":
        print(json.dumps(d.status(), indent=1, default=str))
        return 0
    if a.cmd == "install":
        if os.name == "nt":
            args = winsys.task_scheduler_args(project)
            if a.dry:
                print(" ".join(args))
                return 0
            return subprocess.call(args)
        if sys.platform == "darwin":
            plist = winsys.launchd_plist(project)
            path = Path.home() / "Library" / "LaunchAgents" / "com.orca.daemon.plist"
            if a.dry:
                print(plist)
                return 0
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plist, encoding="utf-8")
            return subprocess.call(["launchctl", "load", "-w", str(path)])
        print(winsys.systemd_unit(project))
        print("# save as ~/.config/systemd/user/orca.service, then: systemctl --user enable --now orca", file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
