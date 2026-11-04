"""Department manager: plan -> run workers -> answer clarifications -> review.

Managers are stateless between calls: every decision is built from the resume brief
(memory files) plus the ticket tree in the store, so a restarted manager picks up
exactly where the old one stopped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent import parse_reply, run_ticket, ticket_prompt
from .memory import Memory
from .providers import Provider
from .store import Store

ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"

MANAGER_PROTOCOL = """\
Reply with exactly one JSON object:
  {"action": "delegate", "notes": "<journal entry>", "tickets": [
      {"title": "...", "worker_role": "...", "context": "...only what the worker needs...",
       "acceptance_criteria": ["..."], "return_spec": {"format": "...", "description": "..."},
       "rigor": "quick_and_dirty|balanced|rigorous"}]}
  {"action": "done", "notes": "...", "result": {...}}      # shaped as your ticket's return_spec
  {"action": "answer", "answer": "..."}                    # only when answering a worker's question
  {"action": "ask", "question": "..."}                     # you need clarification from above
  {"action": "escalate", "reason": "..."}                  # safety issue or something above your authority
  {"action": "fail", "reason": "..."}
Keep each worker ticket narrow. Match effort to the ticket's rigor."""


def load_role(name: str, default: str = "") -> str:
    for p in (ROLES_DIR / f"{name}.md", ROLES_DIR / "workers" / f"{name}.md"):
        if p.exists():
            return p.read_text(encoding="utf-8")
    return default or f"You are a focused {name.replace('_', ' ')} worker. Do exactly what the ticket asks."


class Manager:
    def __init__(self, name: str, store: Store, provider: Provider, cfg: dict[str, Any],
                 workspace: str | Path, role_text: str, manager_models: list[str],
                 worker_models: list[str]) -> None:
        self.name = name
        self.store, self.provider, self.cfg = store, provider, cfg
        self.memory = Memory(workspace, name)
        self.role_text = role_text
        self.manager_models, self.worker_models = manager_models, worker_models

    # ----------------------------------------------------------------- helpers

    def _decide(self, user: str) -> dict[str, Any] | None:
        system = (f"{self.role_text}\n\n# Your memory\n{self.memory.resume_brief()}\n\n{MANAGER_PROTOCOL}")
        limit = self.cfg["policies"]["capability_retry_limit"]
        msgs: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for attempt in range(limit):
            model = self.manager_models[min(attempt, len(self.manager_models) - 1)]
            c = self.provider.complete(model=model, system=system, messages=msgs)
            self.store.add_event(None, self.name, "usage", {
                "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})
            if c.refused:
                return {"action": "escalate", "reason": f"model refusal ({c.refusal_category})"}
            reply = parse_reply(c.text)
            if reply:
                return reply
            msgs = msgs + [{"role": "assistant", "content": c.text or "(empty)"},
                           {"role": "user", "content": "Invalid reply. Respond with exactly one JSON object."}]
        return None

    def _children_summary(self, ticket_id: str) -> str:
        lines = []
        for ch in self.store.list_tickets(parent_id=ticket_id):
            res = json.dumps(ch["result"]) if ch["result"] is not None else "-"
            lines.append(f"- [{ch['status']}] {ch['id']} {ch['title']} -> {res}")
        return "\n".join(lines) or "(no worker tickets)"

    def _apply(self, ticket_id: str, d: dict[str, Any]) -> bool:
        """Apply a terminal-ish decision to the parent ticket. Returns True if the ticket is settled."""
        a = d.get("action")
        if d.get("notes"):
            self.memory.append_journal(f"[{ticket_id}] {d['notes']}")
        if a == "done":
            self.store.transition(ticket_id, "done", self.name, result=d.get("result") or {})
        elif a == "fail":
            self.store.transition(ticket_id, "failed", self.name, note=str(d.get("reason", "")))
        elif a == "escalate":
            self.store.transition(ticket_id, "escalated", self.name, note=str(d.get("reason", "")))
        elif a == "ask":
            self.store.ask_question(ticket_id, self.name, str(d.get("question", "")))
        else:
            return False
        return True

    def _delegate(self, ticket_id: str, d: dict[str, Any]) -> None:
        cap = self.cfg["policies"]["max_children_per_agent"]
        for spec in d.get("tickets", []):
            spec = dict(spec)
            spec["assignee"] = f"worker:{spec.get('worker_role', 'worker')}"
            spec.setdefault("rigor", self.store.get_ticket(ticket_id).get("rigor", "balanced"))
            self.store.create_ticket(spec, actor=self.name, parent_id=ticket_id, max_children=cap)

    # -------------------------------------------------------------------- work

    def work(self, ticket_id: str, max_rounds: int = 6) -> dict[str, Any]:
        """Drive one ticket assigned to this manager until it settles or needs outside input."""
        t = self.store.get_ticket(ticket_id)
        if t["status"] == "open":
            self.store.transition(ticket_id, "in_progress", self.name, note="manager picked up")
        for _ in range(max_rounds):
            t = self.store.get_ticket(ticket_id)
            if t["status"] != "in_progress":
                break
            children = self.store.list_tickets(parent_id=ticket_id)

            # 1. run any open workers
            open_kids = [c for c in children if c["status"] == "open"]
            if open_kids:
                for c in open_kids:
                    run_ticket(self.store, self.provider, c["id"], c["assignee"],
                               load_role(c["worker_role"]), self.worker_models,
                               self.cfg["policies"]["capability_retry_limit"])
                continue

            # 2. answer worker questions
            asking = [c for c in children if c["status"] == "needs_clarification"]
            if asking:
                for c in asking:
                    q = next(q for q in c["questions"] if "answer" not in q)
                    d = self._decide(f"{ticket_prompt(t)}\n\nA worker on ticket {c['id']} asks: {q['question']}")
                    if d and d.get("action") == "answer":
                        self.store.answer_question(c["id"], self.name, str(d.get("answer", "")))
                    elif d and self._apply(ticket_id, d):
                        return self.store.get_ticket(ticket_id)  # asked/escalated upward instead
                    else:
                        self.store.transition(c["id"], "escalated", self.name, note="manager could not answer")
                continue

            # 3. plan (no children yet) or review (all children settled)
            if not children:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\nPlan the work.")
            else:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\n# Worker results\n"
                                 f"{self._children_summary(ticket_id)}\n\nReview, then finish or delegate follow-ups.")
            if d is None:
                self.store.transition(ticket_id, "escalated", self.name, note="no valid decision from model")
                break
            if d.get("action") == "delegate":
                self._delegate(ticket_id, d)
                if d.get("notes"):
                    self.memory.append_journal(f"[{ticket_id}] {d['notes']}")
                continue
            if not self._apply(ticket_id, d):
                self.store.transition(ticket_id, "escalated", self.name, note=f"unknown action {d.get('action')}")
            break
        return self.store.get_ticket(ticket_id)
