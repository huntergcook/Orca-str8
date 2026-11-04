"""Top manager: the only agent that talks to the human.

Each turn's context is rebuilt from files (role + state + human log tail + ticket board),
never from an accumulating chat transcript, so long sessions don't need compaction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent import parse_reply
from .config import model_chain
from .manager import ROLES_DIR, Manager, load_role
from .hardware import JobRunner
from .limits import Halted
from .memory import Memory, human_log
from .providers import Provider
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
     {"type": "finish", "summary": "<why the goal is sufficiently met>"}]}
"say" is required; "actions" may be empty. Ask the human questions when the goal, stopping
criteria, or acceptable rigor is unclear."""


class TopManager:
    def __init__(self, store: Store, provider: Provider, cfg: dict[str, Any], workspace: str | Path) -> None:
        self.store, self.provider, self.cfg = store, provider, cfg
        self.workspace = Path(workspace)
        self.memory = Memory(workspace, "top_manager")
        self.log_path = human_log(workspace)
        self.models = model_chain(cfg, "top")
        self.finished = False
        mgr_models, wrk_models = model_chain(cfg, "manager"), model_chain(cfg, "worker")
        self.jobs = JobRunner(store, workspace)
        vcfg = cfg["verification"]
        verifier_models = list(vcfg.get("verifier_models") or []) or mgr_models
        verifier = Verifier(store, provider, cfg, workspace, verifier_models) if vcfg.get("enabled") else None
        self.managers: dict[str, Manager] = {}
        for dept, spec in cfg["departments"].items():
            role_path = Path(spec.get("manager_role", ""))
            role = (ROLES_DIR.parent / role_path).read_text(encoding="utf-8") if role_path.name and (
                ROLES_DIR.parent / role_path).exists() else load_role("department_manager.template").replace(
                "{{DEPARTMENT}}", dept).replace("{{DEPARTMENT_MISSION}}", spec.get("mission", dept))
            is_verification = dept == "verification"
            diverse = bool(spec.get("diverse_models", False)) and len(verifier_models) > 1
            self.managers[dept] = Manager(f"{dept}_manager", store, provider, cfg, workspace, role,
                                          mgr_models, verifier_models if diverse else wrk_models,
                                          can_acquire_tools=bool(spec.get("can_acquire_tools", False)),
                                          can_use_hardware=bool(spec.get("resource_leases", False)),
                                          jobs=self.jobs,
                                          verifier=None if is_verification else verifier,
                                          diverse_workers=diverse)

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

    def turn(self, human_text: str) -> str:
        self._append_log("human", human_text)
        system = (f"{load_role('top_manager')}\n\n# Departments\n{', '.join(self.managers) or '(none configured)'}\n\n"
                  f"# Your state notes\n{self.memory.read_state() or '(none yet)'}\n\n"
                  f"# Recent conversation with the human\n{self._tail_log()}\n\n"
                  f"# Ticket board\n{self.board()}\n\n{TOP_PROTOCOL}")
        reply, say = None, ""
        for attempt in range(self.cfg["policies"]["capability_retry_limit"]):
            model = self.models[min(attempt, len(self.models) - 1)]
            c = self.provider.complete(model=model, system=system,
                                       messages=[{"role": "user", "content": human_text}])
            self.store.add_event(None, "top_manager", "usage", {
                "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})
            if c.refused:
                say = (f"[The model declined this request ({c.refusal_category}). I will not route around "
                       "that automatically; please rephrase with more context or decide how to proceed.]")
                self.store.add_event(None, "top_manager", "refusal_escalated", {"category": c.refusal_category})
                self._append_log("top_manager", say)
                return say
            reply = parse_reply(c.text, key="say")
            if reply:
                break
        if not reply:
            say = "[I could not produce a valid response; please try again.]"
            self._append_log("top_manager", say)
            return say
        say = str(reply.get("say", ""))
        self._append_log("top_manager", say)
        for act in reply.get("actions", []):
            self._act(act)
        self.pump()
        return say

    def create_ticket(self, a: dict[str, Any]) -> str | None:
        """Create a top-level ticket for a department manager. Returns the ticket id."""
        dept = a.get("department")
        if dept not in self.managers:
            self.store.add_event(None, "top_manager", "bad_action", {"unknown_department": dept})
            return None
        fields = {k: a[k] for k in ("title", "context", "acceptance_criteria", "return_spec", "rigor",
                                    "worker_role", "workspace_refs") if k in a}
        fields.setdefault("worker_role", "worker")
        fields["assignee"] = self.managers[dept].name
        fields["cc"] = ["top_manager"]
        return self.store.create_ticket(fields, actor="top_manager")["id"]

    def _act(self, a: dict[str, Any]) -> None:
        kind = a.get("type")
        if kind == "create_ticket":
            self.create_ticket(a)
        elif kind == "set_state":
            self.memory.write_state(str(a.get("text", "")))
        elif kind == "log":
            self.memory.append_journal(str(a.get("text", "")))
        elif kind == "finish":
            self.memory.append_journal(f"FINISHED: {a.get('summary', '')}")
            self.finished = True

    def pump(self) -> None:
        """Run department managers on their open top-level tickets. Repeats a few passes
        because workers can file tickets to other departments (e.g. tool requests) mid-run."""
        for _ in range(5):
            ran = False
            for m in self.managers.values():
                for status in ("open", "in_progress"):
                    for t in self.store.list_tickets(status=status, assignee=m.name):
                        try:
                            m.work(t["id"])
                        except Halted:
                            raise                      # pause / budget: stop the whole run cleanly
                        except Exception as e:         # provider outage, bad key, bug: never fail silently
                            self._work_failed(t["id"], m.name, e)
                        ran = True
            if not ran:
                break

    def _work_failed(self, ticket_id: str, actor: str, err: Exception) -> None:
        """A manager crashed mid-ticket. Escalate it and its unfinished workers so the failure is
        visible (Needs human) instead of leaving tickets stuck 'in_progress'."""
        note = f"run error: {type(err).__name__}: {err}"[:500]
        self.store.add_event(ticket_id, actor, "run_error", {"error": note})
        active = ("open", "in_progress", "needs_clarification", "blocked")
        for kid in self.store.list_tickets(parent_id=ticket_id):
            if kid["status"] in active:               # the manager can pick these up again once the run restarts
                self.store.escalate(kid["id"], actor, note, kind="technical", to="manager")
        if self.store.get_ticket(ticket_id)["status"] in active:   # a crash is the top manager's to fix
            self.store.escalate(ticket_id, actor, note, kind="technical", to="top_manager")
