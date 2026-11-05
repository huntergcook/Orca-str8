"""Director: the only agent that talks to the human.

Each turn's context is rebuilt from files (role + state + human log tail + ticket board),
never from an accumulating chat transcript, so long sessions don't need compaction.
"""
from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from . import actions
from .agent import parse_reply
from .config import model_chain, project_rules
from .manager import ROLES_DIR, Manager, load_role
from .hardware import JobRunner
from .limits import Halted
from .memory import Memory, human_log
from .providers import Provider, usable
from .store import Store
from .verification import Verifier

TOP_PROTOCOL = """\
Reply with exactly one JSON object:
  {"say": "<message to the human>",
   "actions": [
     {"type": "create_ticket", "department": "<dept>", "title": "...", "context": "...",
      "acceptance_criteria": ["..."], "return_spec": {"format": "...", "description": "..."},
      "rigor": "quick_and_dirty|balanced|rigorous", "worker_role": "<role the dept manager should use>"},
     {"type": "set_state", "text": "<rewrite your state notes: goal, stopping criteria, rigor profile, constraints>"},
     {"type": "log", "text": "<durable note about a human decision or instruction>"},
     {"type": "answer_ticket", "ticket_id": "...", "text": "<answer to a question a manager is blocked on>"},
     {"type": "retry_ticket", "ticket_id": "...", "text": "<guidance for the retry>"},
     {"type": "cancel_ticket", "ticket_id": "...", "text": "<why>"},
     {"type": "assume", "text": "<an assumption you made instead of asking the human; they can overturn it later>"},
     {"type": "ask_human", "question": "<only a question that truly needs the human; work continues meanwhile>"},
     {"type": "finish", "summary": "<why the goal is sufficiently met>"}]}
"say" is required; "actions" may be empty. Ask the human questions when the goal, stopping
criteria, or acceptable rigor is unclear (during intake), but when the human is away prefer `assume` over `ask_human`
unless the mission brief says otherwise or the matter is safety, spending or a refusal."""


class Director:
    def __init__(self, store: Store, provider: Provider, cfg: dict[str, Any], workspace: str | Path) -> None:
        self.store, self.provider, self.cfg = store, provider, cfg
        self.workspace = Path(workspace)
        legacy, new = self.workspace / "memory" / "top_manager", self.workspace / "memory" / "director"
        if legacy.is_dir() and not new.exists():          # projects from before the rename: keep the director's notes
            legacy.rename(new)
        self.memory = Memory(workspace, "director")
        self.log_path = human_log(workspace)
        self.models = model_chain(cfg, "director")
        self.finished = False
        self.finish_gate: Any = None              # daemon: callable -> (ok, reason); a finish only counts if ok
        self.finish_rejection: str | None = None  # why the last finish request was refused (fed to the next turn)
        self.notify: Any = None                   # daemon: callable(level, title, body)
        mgr_models, wrk_models = model_chain(cfg, "manager"), model_chain(cfg, "worker")
        self.jobs = JobRunner(store, workspace)
        vcfg = cfg["verification"]
        verifier_models = list(vcfg.get("verifier_models") or []) or mgr_models
        verifier = Verifier(store, provider, cfg, workspace, verifier_models) if vcfg.get("enabled") else None
        self.managers: dict[str, Manager] = {}
        departments = {dept: f"{dept}_manager" for dept in cfg["departments"]}
        for dept, spec in cfg["departments"].items():
            role_path = Path(spec.get("manager_role", ""))
            found = next((c for c in (Path.cwd() / role_path, ROLES_DIR.parent / role_path)
                          if role_path.name and c.is_file()), None)       # project file first, then built-in
            role = found.read_text(encoding="utf-8") if found else load_role("department_manager.template").replace(
                "{{DEPARTMENT}}", dept).replace("{{DEPARTMENT_MISSION}}", spec.get("mission", dept))
            is_verification = dept == "verification"
            diverse = bool(spec.get("diverse_models", False)) and len(verifier_models) > 1
            self.managers[dept] = Manager(f"{dept}_manager", store, provider, cfg, workspace, role,
                                          mgr_models, verifier_models if diverse else wrk_models,
                                          can_acquire_tools=bool(spec.get("can_acquire_tools", False)),
                                          can_use_hardware=bool(spec.get("resource_leases", False)),
                                          jobs=self.jobs,
                                          verifier=None if is_verification else verifier,
                                          diverse_workers=diverse, writes_code=spec.get("writes_code"),
                                          departments=departments)

    # ------------------------------------------------------------------ context

    def board(self) -> str:
        rows = []
        for t in self.store.list_tickets():
            if t["parent_id"] is None:
                res = f" -> {t['result']}" if t["result"] is not None else ""
                rows.append(f"- [{t['status']}] {t['id']} ({t['assignee']}) {t['title']}{res}")
        return "\n".join(rows) or "(no tickets yet)"

    def _tail_log(self, n: int = 6000) -> str:
        return self.log_path.read_text(encoding="utf-8")[-n:] if self.log_path.exists() else ""

    def _append_log(self, who: str, text: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n**{who}:** {text.strip()}\n")

    # --------------------------------------------------------------------- turn

    def _system_prompt(self) -> str:
        rules = project_rules(self.cfg)
        rules_section = f"# Project rules and mission brief (binding)\n{rules}\n\n" if rules else ""
        return (f"{load_role('director')}\n\n{rules_section}# Departments\n{', '.join(self.managers) or '(none configured)'}\n\n"
                f"# Your state notes\n{self.memory.read_state() or '(none yet)'}\n\n"
                f"# Recent conversation with the human\n{self._tail_log()}\n\n"
                f"# Ticket board\n{self.board()}\n\n{TOP_PROTOCOL}")

    def _ask(self, user_text: str, speaker: str) -> str:
        """One director decision: build the context from files (never from a transcript), call the model chain, apply
        the actions. `speaker` is who the log shows as having said `user_text`."""
        self._append_log(speaker, user_text)
        system = self._system_prompt()
        reply, say = None, ""
        for attempt in range(self.cfg["policies"]["capability_retry_limit"]):
            avail = usable(self.models) or self.models
            model = avail[min(attempt, len(avail) - 1)]
            c = self.provider.complete(model=model, system=system,
                                       messages=[{"role": "user", "content": user_text}])
            self.store.record_usage(None, "director", "director", c)
            if c.refused:
                say = (f"[The model declined this request ({c.refusal_category}). I will not route around "
                       "that automatically; please rephrase with more context or decide how to proceed.]")
                self.store.add_event(None, "director", "refusal_escalated", {"category": c.refusal_category})
                self._append_log("director", say)
                return say
            reply = parse_reply(c.text, key="say")
            if reply:
                break
        if not reply:
            say = "[I could not produce a valid response; please try again.]"
            self._append_log("director", say)
            return say
        say = str(reply.get("say", ""))
        self._append_log("director", say)
        for act in reply.get("actions", []):
            self._act(act)
        return say

    def turn(self, human_text: str) -> str:
        """The human speaks to the built-in director (python -m orchestrator)."""
        say = self._ask(human_text, "human")
        self.pump()
        return say

    def event_turn(self, kind: str, text: str) -> str:
        """The daemon wakes the director for an event (ticket finished, idle, stalled...). No human is speaking and
        the daemon, not this call, runs the departments afterwards."""
        self.store.add_event(None, "director", "director_turn", {"event": kind})
        return self._ask(f"[system event: {kind}]\n{text}", "system")

    def create_ticket(self, a: dict[str, Any]) -> str | None:
        """Create a top-level ticket for a department manager. Returns the ticket id."""
        dept = a.get("department")
        if dept not in self.managers:
            self.store.add_event(None, "director", "bad_action", {"unknown_department": dept})
            return None
        fields = {k: a[k] for k in ("title", "context", "acceptance_criteria", "return_spec", "rigor",
                                    "worker_role", "workspace_refs", "worker_tickets", "code") if k in a}
        fields.setdefault("worker_role", "worker")
        fields["assignee"] = self.managers[dept].name
        fields["cc"] = ["director"]
        return self.store.create_ticket(fields, actor="director")["id"]

    def _act(self, a: dict[str, Any]) -> None:
        kind = a.get("type")
        try:
            self._act_inner(kind, a)
        except Exception as e:                      # one bad action must not lose the rest of the director's turn
            self.store.add_event(None, "director", "bad_action", {"type": kind, "error": f"{type(e).__name__}: {e}"[:300]})

    def _act_inner(self, kind: str | None, a: dict[str, Any]) -> None:
        if kind == "create_ticket":
            self.create_ticket(a)
        elif kind == "set_state":
            self.memory.write_state(str(a.get("text", "")))
        elif kind == "log":
            self.memory.append_journal(str(a.get("text", "")))
        elif kind == "answer_ticket":
            actions.answer(self.store, str(a["ticket_id"]), str(a.get("text", "")), "director")
        elif kind == "retry_ticket":
            actions.retry(self.store, str(a["ticket_id"]), str(a.get("text", "")), "director")
        elif kind == "cancel_ticket":
            actions.cancel(self.store, str(a["ticket_id"]), str(a.get("text", "")), "director")
        elif kind == "assume":
            text = str(a.get("text", "")).strip()
            self.memory.append_journal(f"ASSUMPTION (human not consulted): {text}")
            self.store.add_event(None, "director", "assumption", {"text": text[:500]})
        elif kind == "ask_human":
            q = str(a.get("question", "")).strip()
            if q:
                self._append_log("director", f"QUESTION FOR THE HUMAN: {q}")
                self.store.add_event(None, "director", "human_question", {"question": q[:500]})
                if self.notify:
                    self.notify("question", "The director has a question", q)
        elif kind == "finish":
            summary = str(a.get("summary", ""))
            if self.finish_gate is not None:
                ok, why = self.finish_gate()
                if not ok:
                    self.finish_rejection = why
                    self.memory.append_journal(f"FINISH REFUSED (goal not verifiably met): {why}")
                    self.store.add_event(None, "director", "finish_refused", {"reason": why[:500]})
                    return
            self.finish_rejection = None
            self.memory.append_journal(f"FINISHED: {summary}")
            self.finished = True

    def pump(self) -> None:
        """Run every department's tickets in parallel, picking up new tickets as soon as they appear (rework, requests
        between departments, tool requests), until nothing is runnable. A pause or exhausted budget stops it cleanly."""
        limit = max(1, int(self.cfg["policies"].get("max_parallel_tickets", 8)))
        running: dict[str, Future] = {}
        halted: list[Halted] = []
        keepalive = bool(self.cfg["policies"].get("keepalive", False))
        beat_secs = 60 * float(self.cfg["policies"].get("heartbeat_minutes", 30))
        last_beat: dict[str, float] = {}               # department -> monotonic time of its last status check
        watch = {"at": 0.0, "warned": {}}              # watchdog state: last scan, ticket id -> last warned event seq

        not_before: dict[str, float] = {}              # ticket id -> monotonic time before which it is not restarted

        def one(m: Manager, ticket_id: str) -> None:
            try:
                m.work(ticket_id)
            except Halted as h:
                halted.append(h)               # pause / budget: stop the whole run cleanly
            except Exception as e:             # provider outage, bad key, bug: never fail silently
                note = f"run error: {type(e).__name__}: {e}"[:500]
                self.store.add_event(ticket_id, m.name, "run_error", {"error": note})
                if not self._restart(ticket_id, note, not_before):    # persistence first: retry before escalating
                    self._work_failed(ticket_id, m.name, e)

        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="ticket") as pool:
            while True:
                try:
                    actions.release_abandoned_holds(self.store, "director")   # e.g. a cancelled ticket's job ended
                except Exception:
                    pass
                if not halted:
                    for m in self.managers.values():
                        for status in ("open", "in_progress"):
                            for t in self.store.list_tickets(status=status, assignee=m.name):
                                if (t["id"] not in running and t["parent_id"] is None
                                        and time.monotonic() >= not_before.get(t["id"], 0.0)):
                                    running[t["id"]] = pool.submit(one, m, t["id"])
                if not halted:
                    self._watchdog(watch)
                if keepalive and not halted and not self.finished:
                    self._heartbeats(last_beat, beat_secs)
                    if not running:                    # idle: stay alive, rescan for new tickets and heartbeats
                        time.sleep(5)
                        continue
                if not running and not halted and any(v > time.monotonic() for v in not_before.values()):
                    time.sleep(2)                      # everything left is backing off before a restart: wait, do not exit
                    continue
                if not running:
                    break
                wait(list(running.values()), return_when=FIRST_COMPLETED, timeout=5)
                for tid in [k for k, f in running.items() if f.done()]:
                    running.pop(tid)
                    # a ticket that is somehow still in progress after its manager returned would be resubmitted
                    # forever; escalate it instead
                    t = self.store.get_ticket(tid)
                    if t["status"] == "in_progress" and not halted:
                        if not self._restart(tid, "its manager stopped without an outcome", not_before):
                            self.store.escalate(tid, t["assignee"], "its manager stopped without an outcome "
                                                "(restarts exhausted)", kind="technical", to="director")
        if halted:
            raise halted[0]

    def _restart(self, ticket_id: str, reason: str, not_before: dict[str, float]) -> bool:
        """Persistence: a manager that stopped or crashed is restarted (with exponential backoff) instead of being
        escalated at once. Its state lives in files and the ticket tree, so the restart resumes where it stopped.
        Returns False once `policies.max_restarts` restarts of this ticket are used up; the caller then escalates."""
        pol = self.cfg["policies"]
        used = sum(1 for e in self.store.events(ticket_id) if e["type"] == "restarted")
        if used >= int(pol.get("max_restarts", 6)):
            return False
        delay = min(float(pol.get("restart_backoff_seconds", 5)) * (2 ** used), 600.0)
        self.store.add_event(ticket_id, "orchestrator", "restarted", {"n": used + 1, "reason": reason[:300],
                                                                      "delay_s": delay})
        not_before[ticket_id] = time.monotonic() + delay
        return True

    def _watchdog(self, watch: dict[str, Any]) -> None:
        """Everyone must be working all the time: every minute, find tickets 'in_progress' that have had no event for
        `stall_minutes` and flag them. The flag is a 'stalled' event on the ticket (managers see it in their ticket events
        and the heartbeat status check) plus a director journal line; nothing is escalated or killed, because a
        long hardware job may be healthy. Each stall is flagged once per quiet period."""
        from datetime import datetime, timezone
        now = time.monotonic()
        if now - watch["at"] < 60:
            return
        watch["at"] = now
        limit = 60 * float(self.cfg["policies"].get("stall_minutes", 30))
        try:
            for t in self.store.list_tickets(status="in_progress"):
                evs = self.store.events(t["id"])
                last = evs[-1] if evs else None
                if last is None or watch["warned"].get(t["id"]) == last["seq"]:
                    continue
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds()
                for a in self.store.list_agents():     # an agent's heartbeat counts as activity
                    if a["ticket_id"] == t["id"] and a["state"] not in ("done", "failed", "escalated", "cancelled", "stopped"):
                        age = min(age, (datetime.now(timezone.utc) - datetime.fromisoformat(a["updated_at"])).total_seconds())
                if age < limit:
                    continue
                watch["warned"][t["id"]] = last["seq"]
                why = f"watchdog: no activity for {int(age // 60)} min (last event: {last['type']} by {last['actor']})"
                self.store.add_event(t["id"], "watchdog", "stalled", {"minutes": int(age // 60)})
                self.memory.append_journal(f"{why}; ticket {t['id']} '{t['title'][:60]}' assignee {t['assignee']}")
                if self.cfg["policies"].get("watchdog_acts", True) and age >= 2 * limit:
                    # deal with it: a worker that is dead for twice the stall time goes back to its manager (who retries or
                    # replaces it); a department ticket that is dead goes to the director. Never while it holds a device.
                    ids = {t["id"], *[k["id"] for k in self.store.list_tickets(parent_id=t["id"])]}
                    if any(r.get("holder_ticket") in ids for r in self.store.list_resources()):
                        continue
                    self.store.escalate(t["id"], "watchdog", why + "; acting on it", kind="technical",
                                        to="director" if t["parent_id"] is None else "manager")
        except Exception:
            pass                                       # the watchdog must never take the run down

    def _heartbeats(self, last_beat: dict[str, float], beat_secs: float) -> None:
        """Keepalive: every `heartbeat_minutes` each department manager gets a status-check ticket (unless it already
        has one open), so managers keep the work moving instead of the run ending when departments go idle."""
        now = time.monotonic()
        for dept, m in self.managers.items():
            if now - last_beat.get(dept, -beat_secs) < beat_secs:
                continue
            last_beat[dept] = now
            mine = self.store.list_tickets(assignee=m.name)
            if any(t["title"].startswith("Status check") and t["status"] in ("open", "in_progress") for t in mine):
                continue
            self.store.create_ticket({
                "title": f"Status check: {dept}",
                "context": (
                    f"Periodic heartbeat for the {dept} department; you are a standing manager, not a one-shot worker. "
                    "1) Read your memory state/journal and the project goal. 2) Review every ticket and worker in your "
                    "department: anything in progress with no recent events, failed, escalated or waiting on a question "
                    "is yours to unblock now (retry with guidance, change model, split, or escalate with a clear reason). "
                    "3) Triage follow_ups of your finished tickets. 4) If your department has no open work, propose the "
                    "next most valuable tickets toward the goal as a concise list in your result (the director "
                    "creates them). 5) Prefer open-source code, libraries and public documentation (use the web fetch "
                    "tool when registered) over inventing from scratch; record licenses. Keep this check short."),
                "acceptance_criteria": ["every stuck or failed item in the department has an owner and a next step",
                                        "next-work proposals listed, or 'none needed' with a reason"],
                "return_spec": {"format": "short report", "description": "stuck items handled, proposals"},
                "rigor": "quick_and_dirty", "worker_role": "worker", "assignee": m.name, "cc": ["director"]},
                actor="orchestrator")

    def _work_failed(self, ticket_id: str, actor: str, err: Exception) -> None:
        """A manager crashed mid-ticket. Escalate it and its unfinished workers so the failure is
        visible (Needs human) instead of leaving tickets stuck 'in_progress'."""
        note = f"run error: {type(err).__name__}: {err}"[:500]
        self.store.add_event(ticket_id, actor, "run_error", {"error": note})
        active = ("open", "in_progress", "needs_clarification", "blocked")
        for kid in self.store.list_tickets(parent_id=ticket_id):
            if kid["status"] in active:               # the manager can pick these up again once the run restarts
                self.store.escalate(kid["id"], actor, note, kind="technical", to="manager")
        if self.store.get_ticket(ticket_id)["status"] in active:   # a crash is the director's to fix
            self.store.escalate(ticket_id, actor, note, kind="technical", to="director")
