"""Department manager: plan -> run workers -> answer clarifications -> review.

Managers are stateless between calls: every decision is built from the resume brief
(memory files) plus the ticket tree in the store, so a restarted manager picks up
exactly where the old one stopped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent import as_result, parse_reply, run_ticket, ticket_prompt
from .memory import Memory
from .providers import Provider
from .store import Store, StoreError
from . import actions
from .hardware import JobRunner, hardware_brief
from .tools import ToolBox
from .verification import Verifier

ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"

MANAGER_PROTOCOL = """\
Reply with exactly one JSON object:
  {"action": "delegate", "notes": "<journal entry>", "tickets": [
      {"title": "...", "worker_role": "...", "context": "...only what the worker needs...",
       "acceptance_criteria": ["..."], "return_spec": {"format": "...", "description": "..."},
       "rigor": "quick_and_dirty|balanced|rigorous"}]}
  {"action": "done", "notes": "...", "result": {...},       # result shaped as your ticket's return_spec
   "override_reason": "..."}                                # only if finishing despite verification concerns
  {"action": "answer", "answer": "..."}                    # only when answering a worker's question
  {"action": "ask", "question": "..."}                     # you need clarification from above
  {"action": "retry_workers", "notes": "...", "retries": [{"ticket_id": "<escalated worker ticket>",
      "guidance": "what to do differently this time"}]}
  {"action": "escalate", "kind": "safety" | "blocked", "reason": "..."}
  {"action": "fail", "reason": "..."}
Keep each worker ticket narrow. Match effort to the ticket's rigor.

YOU OWN WORKER PROBLEMS. When a worker ticket is [escalated] or [failed], read its reason and the sample of its
last bad reply, then fix it yourself: retry_workers with specific guidance (a retry automatically uses the next
fallback model; each worker has a limited number of retries), delegate a differently-scoped replacement, or
accept the gap and finish. Typical fixes: tell it to save long content to a file and reply briefly, narrow its
scope, or give it the missing information. Use "escalate" only for what you truly cannot resolve (it goes to the
top manager, and only from there possibly to the human). Use kind "safety" only for real safety concerns, which go
straight to the human."""


def load_role(name: str, default: str = "") -> str:
    """A role prompt by name. The project's own roles/ folder (the working directory) is searched first,
    so a project can override or add roles; then the orchestrator's built-in roles/."""
    for base in (Path.cwd() / "roles", ROLES_DIR):
        for p in (base / f"{name}.md", base / "workers" / f"{name}.md"):
            if p.exists():
                return p.read_text(encoding="utf-8")
    return default or f"You are a focused {name.replace('_', ' ')} worker. Do exactly what the ticket asks."


class Manager:
    def __init__(self, name: str, store: Store, provider: Provider, cfg: dict[str, Any],
                 workspace: str | Path, role_text: str, manager_models: list[str],
                 worker_models: list[str], can_acquire_tools: bool = False,
                 can_use_hardware: bool = False, jobs: JobRunner | None = None,
                 verifier: Verifier | None = None, diverse_workers: bool = False) -> None:
        self.name = name
        self.verifier, self.diverse_workers = verifier, diverse_workers
        self.workspace = Path(workspace)
        self.can_acquire_tools = can_acquire_tools
        self.can_use_hardware = can_use_hardware
        self.jobs = jobs if jobs is not None else JobRunner(store, workspace)
        self.store, self.provider, self.cfg = store, provider, cfg
        self.memory = Memory(workspace, name)
        self.role_text = role_text
        self.manager_models, self.worker_models = manager_models, worker_models

    # ----------------------------------------------------------------- helpers

    def _decide(self, user: str) -> dict[str, Any] | None:
        hw = hardware_brief(self.store) if self.can_use_hardware else ""
        hw_section = f"# Hardware status (you own this)\n{hw}\n\n" if hw else ""
        system = (f"{self.role_text}\n\n# Your memory\n{self.memory.resume_brief()}\n\n"
                  f"{hw_section}{MANAGER_PROTOCOL}")
        limit = self.cfg["policies"]["capability_retry_limit"]
        msgs: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for attempt in range(limit):
            model = self.manager_models[min(attempt, len(self.manager_models) - 1)]
            c = self.provider.complete(model=model, system=system, messages=msgs)
            self.store.add_event(None, self.name, "usage", {
                "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})
            if c.refused:
                return {"action": "escalate", "kind": "refusal", "reason": f"model refusal ({c.refusal_category})"}
            reply = parse_reply(c.text)
            if reply:
                return reply
            msgs = msgs + [{"role": "assistant", "content": c.text or "(empty)"},
                           {"role": "user", "content": "Invalid reply. Respond with exactly one JSON object."}]
        return None

    def _children_summary(self, ticket_id: str) -> str:
        """One entry per worker. Failed or escalated workers include why, a sample of their last bad
        reply, and how many retries they have used, so the manager can actually diagnose and fix them."""
        max_retries = self.cfg["policies"]["max_worker_retries"]
        lines = []
        for ch in self.store.list_tickets(parent_id=ticket_id):
            res = json.dumps(ch["result"]) if ch["result"] is not None else "-"
            line = f"- [{ch['status']}] {ch['id']} {ch['title']} -> {res}"
            if ch["status"] in ("escalated", "failed"):
                events = self.store.events(ch["id"])
                line += f"\n    reason: {actions.why_from_events(events) or '(none recorded)'}"
                bad = [e for e in events if e["type"] == "invalid_reply"]
                if bad:
                    p = bad[-1]["payload"]
                    line += (f"\n    its last invalid reply ({p['chars']} chars) began: {p['head'][:200]!r}"
                             f" ... and ended: {p['tail'][-120:]!r}")
                line += f"\n    retries used: {actions.retries_used(self.store, ch['id'])}/{max_retries}"
            lines.append(line)
        return "\n".join(lines) or "(no worker tickets)"

    def _close_children(self, ticket_id: str) -> None:
        """When the manager settles its ticket, workers still escalated are resolved by that decision."""
        for ch in self.store.list_tickets(parent_id=ticket_id):
            if ch["status"] == "escalated":
                self.store.transition(ch["id"], "failed", self.name, note="settled by the manager (not retried)")

    def _retry_workers(self, ticket_id: str, d: dict[str, Any]) -> tuple[int, list[str]]:
        """Send escalated workers back with guidance. Returns (retries applied, error messages)."""
        mine = {c["id"]: c for c in self.store.list_tickets(parent_id=ticket_id)}
        max_retries = self.cfg["policies"]["max_worker_retries"]
        applied, errors = 0, []
        for item in d.get("retries") or []:
            wid = str(item.get("ticket_id", ""))
            if wid not in mine:
                errors.append(f"{wid!r} is not one of your workers")
            elif actions.retries_used(self.store, wid) >= max_retries:
                errors.append(f"{wid}: retry limit ({max_retries}) reached; delegate different work or give up on it")
            else:
                try:
                    actions.retry(self.store, wid, str(item.get("guidance", "")), self.name)
                    applied += 1
                except StoreError as e:
                    errors.append(f"{wid}: {e}")
        return applied, errors

    def _apply(self, ticket_id: str, d: dict[str, Any]) -> bool:
        """Apply a terminal-ish decision to the parent ticket. Returns True if the ticket is settled."""
        a = d.get("action")
        if d.get("notes"):
            self.memory.append_journal(f"[{ticket_id}] {d['notes']}")
        if a == "done":
            self._close_children(ticket_id)
            self.store.transition(ticket_id, "done", self.name, result=as_result(d.get("result")))
        elif a == "fail":
            self._close_children(ticket_id)
            self.store.transition(ticket_id, "failed", self.name, note=str(d.get("reason", "")))
        elif a == "escalate":
            kind = d.get("kind") if d.get("kind") in ("safety", "refusal", "blocked", "technical") else "blocked"
            self.store.escalate(ticket_id, self.name, str(d.get("reason", "")), kind=kind)
        elif a == "ask":
            self.store.ask_question(ticket_id, self.name, str(d.get("question", "")))
        else:
            return False
        return True

    def _delegate(self, ticket_id: str, d: dict[str, Any]) -> None:
        cap = self.cfg["policies"]["max_children_per_agent"]
        for spec in d.get("tickets", []):
            spec = dict(spec)
            role = spec.get("worker_role", "worker")
            # Number workers per role under this ticket (surveyor-1, surveyor-2, ...) so they can be told apart.
            n = 1 + sum(1 for c in self.store.list_tickets(parent_id=ticket_id) if c["worker_role"] == role)
            spec["assignee"] = f"worker:{role}-{n}"
            spec.setdefault("rigor", self.store.get_ticket(ticket_id).get("rigor", "balanced"))
            self.store.create_ticket(spec, actor=self.name, parent_id=ticket_id, max_children=cap)

    # ------------------------------------------------------------ verification

    def _verify_gate(self, ticket_id: str, d: dict[str, Any]) -> dict[str, Any]:
        """Independent advisory check before a 'done' is accepted. The manager keeps the final say:
        it may fix things, finish anyway (override is logged), or escalate. Rounds are capped."""
        if self.verifier is None:
            return d
        max_rounds = self.cfg["verification"]["max_rounds"]
        verdict = "pass"
        while d.get("action") == "done":
            t = self.store.get_ticket(ticket_id)
            past = [e for e in self.store.events(ticket_id) if e["type"] == "verification"]
            if len(past) >= max_rounds:
                verdict = past[-1]["payload"]["verdict"] if past else "pass"
                break
            report = self.verifier.verify(t, d.get("result"), self.name)
            verdict = report["verdict"]
            if verdict == "pass":
                break
            left = max_rounds - len(past) - 1
            d2 = self._decide(
                f"# Your ticket\n{ticket_prompt(t)}\n\n# Verification report (independent, advisory)\n"
                f"{self.verifier.format_report(report)}\n\nWeigh this heavily; it is written by checkers with the "
                f"broadest view of the goal. You keep the final say. Fix the problems (delegate follow-ups), "
                f"or finish with 'override_reason' if you disagree, or escalate. Verification rounds left: {left}.")
            if d2 is None:
                break
            d = d2
            if d.get("override_reason"):      # manager disagrees and says why: respect the final say
                break
        if d.get("action") == "done" and verdict != "pass":
            self.store.add_event(ticket_id, self.name, "verification_overridden",
                                 {"verdict": verdict, "reason": d.get("override_reason") or "none given"})
        return d

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
            # 'in_progress' children here are orphans from a crash, or a worker the human sent back
            # for another try; only one run is ever active, so re-running them is safe.
            open_kids = [c for c in children if c["status"] in ("open", "in_progress")]
            if open_kids:
                for i, c in enumerate(open_kids):
                    models = self.worker_models
                    if self.diverse_workers and len(models) > 1:      # spread verifiers across models
                        k = (len(children) - len(open_kids) + i) % len(models)
                        models = models[k:] + models[:k]
                    else:                                              # each retry moves to the next fallback model
                        used = actions.retries_used(self.store, c["id"])
                        models = models[min(used, len(models) - 1):]
                    box = ToolBox(self.workspace, actor=c["assignee"], can_acquire=self.can_acquire_tools,
                                  store=self.store, ticket_id=c["id"], can_use_hardware=self.can_use_hardware,
                                  jobs=self.jobs)
                    run_ticket(self.store, self.provider, c["id"], c["assignee"],
                               load_role(c["worker_role"]), models,
                               self.cfg["policies"]["capability_retry_limit"], toolbox=box)
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
                    else:     # the manager is the one stuck, so this goes to the top manager
                        self.store.escalate(c["id"], self.name, "manager could not answer the worker's question",
                                            kind="blocked", to="top_manager")
                continue

            # 3. plan (no children yet) or review (all children settled)
            if not children:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\nPlan the work.")
            else:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\n# Worker results\n"
                                 f"{self._children_summary(ticket_id)}\n\nReview, then finish or delegate follow-ups.")
            if d is None:
                self.store.escalate(ticket_id, self.name, "no valid decision from the manager model", kind="technical")
                break
            if d.get("action") == "retry_workers":
                applied, errors = self._retry_workers(ticket_id, d)
                self.memory.append_journal(f"[{ticket_id}] retried {applied} worker(s)"
                                           + (f"; problems: {'; '.join(errors)}" if errors else "")
                                           + (f". {d['notes']}" if d.get("notes") else ""))
                if applied:
                    continue                  # step 1 re-runs the reopened workers next round
                self.store.escalate(ticket_id, self.name, "could not retry workers: " + ("; ".join(errors) or "none given"),
                                    kind="blocked")
                break
            if d.get("action") == "done":
                d = self._verify_gate(ticket_id, d)
            if d.get("action") == "delegate":
                self._delegate(ticket_id, d)
                if d.get("notes"):
                    self.memory.append_journal(f"[{ticket_id}] {d['notes']}")
                continue
            if not self._apply(ticket_id, d):
                self.store.escalate(ticket_id, self.name, f"unknown action {d.get('action')!r} from the manager model",
                                    kind="technical")
            break
        return self.store.get_ticket(ticket_id)
