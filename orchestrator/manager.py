"""Department manager, run like a team lead.

Every runnable worker runs in parallel; questions are answered and finished code branches reviewed as soon as they
arrive; the manager plans or concludes only when nothing else is pending. Managers have their own tools and full
authority to fix what their workers could not. They are stateless between calls: every decision is built from the
memory files plus the ticket tree, so a restarted manager picks up exactly where the old one stopped.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from . import actions
from .agent import as_result, compact, parse_reply, run_ticket, ticket_prompt
from .codework import (CodeError, CodeSpec, CodeToolBox, branch_state, code_repo_from_cfg, format_gate_failures,
                       glob_match, rework_spec, run_gates)
from .config import project_rules
from .hardware import JobRunner, hardware_brief
from .limits import Halted
from .manager_tools import ManagerToolBox
from .memory import Memory
from .providers import Provider
from .store import Store, StoreError
from .tools import ToolBox
from .verification import Verifier

ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"

MANAGER_PROTOCOL = """\
Reply with exactly one JSON object:
  {"action": "delegate", "notes": "<journal entry>", "tickets": [
      {"title": "...", "worker_role": "...", "context": "...only what the worker needs...",
       "acceptance_criteria": ["..."], "return_spec": {"format": "...", "description": "..."},
       "rigor": "quick_and_dirty|balanced|rigorous",
       "code": {"allowed_paths": ["..."], "acceptance": ["..."], "context_files": ["..."]}}]}   # "code": only for tickets that change code
  {"action": "done", "notes": "...", "result": {...},       # result shaped as your ticket's return_spec
   "override_reason": "..."}                                # only if finishing despite verification concerns
  {"action": "answer", "answer": "..."}                    # only when answering a worker's question
  {"action": "ask", "question": "..."}                     # you need clarification from above
  {"action": "integrate", "notes": "...", "accept": ["<worker ticket id>"],       # Software manager only: you own the repository
      "reject": [{"ticket_id": "<worker ticket id>", "feedback": "what is wrong, where, and what right looks like"}]}
  {"action": "retry_workers", "notes": "...", "retries": [{"ticket_id": "<escalated worker ticket>",
      "guidance": "what to do differently this time"}]}
  {"action": "escalate", "kind": "safety" | "blocked", "reason": "..."}
  {"action": "fail", "reason": "..."}
Keep each worker ticket narrow. Match effort to the ticket's rigor.
Reply with plain JSON text only: never use XML-style or special-token tool-call markup (anything like <|...|> or DSML tags).

YOU OWN WORKER PROBLEMS. When a worker ticket is [escalated] or [failed], read its reason and the sample of its
last bad reply, then fix it yourself: retry_workers with specific guidance (a retry automatically uses the next
fallback model; each worker has a limited number of retries), delegate a differently-scoped replacement, or
accept the gap and finish. Typical fixes: tell it to save long content to a file and reply briefly, narrow its
scope, or give it the missing information. Use "escalate" only for what you truly cannot resolve (it goes to the
top manager, and only from there possibly to the human). Use kind "safety" only for real safety concerns, which go
straight to the human.

YOU HAVE FULL AUTHORITY AND YOUR OWN TOOLS. Before answering you may call tools ({"action": "tool", ...}) to read the
project, check a worker's output, run a test, or fix something yourself: when a small fix is quicker than sending work
back, do it. Use request_work to give other departments work you find that they should do.
NO HOLES: your department's work must be complete. Every acceptance criterion needs real evidence; every follow-up a
worker raised is done, delegated, or passed up. Your final result should include "follow_ups": [...] listing work you
know should happen that you did not do, so the top manager can plan it."""


def load_role(name: str, default: str = "") -> str:
    """A role prompt by name. The project's own roles/ folder (the working directory) is searched first,
    so a project can override or add roles; then the orchestrator's built-in roles/."""
    for base in (Path.cwd() / "roles", ROLES_DIR):
        for p in (base / f"{name}.md", base / "workers" / f"{name}.md"):
            if p.exists():
                return p.read_text(encoding="utf-8")
    return default or f"You are a focused {name.replace('_', ' ')} worker. Do exactly what the ticket asks."



class _Cancelled(Exception):
    """The manager's ticket was cancelled from above while it was deciding."""

class Manager:
    def __init__(self, name: str, store: Store, provider: Provider, cfg: dict[str, Any],
                 workspace: str | Path, role_text: str, manager_models: list[str],
                 worker_models: list[str], can_acquire_tools: bool = False,
                 can_use_hardware: bool = False, jobs: JobRunner | None = None,
                 verifier: Verifier | None = None, diverse_workers: bool = False,
                 writes_code: bool | None = None, departments: dict[str, str] | None = None) -> None:
        self.name = name
        self.department = name.removesuffix("_manager")
        self.verifier, self.diverse_workers = verifier, diverse_workers
        self.workspace = Path(workspace)
        self.can_acquire_tools = can_acquire_tools
        self.can_use_hardware = can_use_hardware
        self.jobs = jobs if jobs is not None else JobRunner(store, workspace)
        self.store, self.provider, self.cfg = store, provider, cfg
        self.memory = Memory(workspace, name)
        self.role_text = role_text
        self.manager_models, self.worker_models = manager_models, worker_models
        # The Software department writes code by default; any department can be given the right in the config.
        self.writes_code = (self.department == "software") if writes_code is None else writes_code
        self.departments = dict(departments or {})
        self._code_repo: Any = False                 # lazily built; None = code tickets not available
        self._notice: dict[str, str] = {}            # feedback for the manager's next decision, per ticket
        pol = cfg["policies"]
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(pol.get("max_parallel_workers", 4))),
                                       thread_name_prefix=f"{self.department}-worker")

    # ---------------------------------------------------------------- status
    def _agent_id(self, ticket_id: str | None) -> str:
        return f"{self.name}@{ticket_id}" if ticket_id else self.name

    def _status(self, ticket_id: str | None, state: str, detail: str = "", model: str | None = None) -> None:
        try:
            self.store.set_agent(self._agent_id(ticket_id), self.name, "manager", self.department, ticket_id, state,
                                 detail, model)
        except Exception:                 # status reporting must never break the work itself
            pass

    @property
    def review_by_manager(self) -> bool:
        return self.writes_code and (self.cfg.get("code") or {}).get("review", "manager") == "manager"

    # ----------------------------------------------------------------- deciding
    def _toolbox(self, ticket_id: str | None) -> ManagerToolBox:
        return ManagerToolBox(
            self.workspace, self.name, store=self.store, ticket_id=ticket_id,
            read_roots=self.cfg.get("read_roots") or [], departments=self.departments,
            can_acquire=self.can_acquire_tools, can_use_hardware=self.can_use_hardware, jobs=self.jobs,
            code_repo=self.code_repo if self.writes_code else None, code_cfg=self.cfg.get("code") or {},
            my_tickets=(lambda: {c["id"] for c in self.store.list_tickets(parent_id=ticket_id)}) if ticket_id else None)

    def _decide(self, user: str, ticket_id: str | None = None, doing: str = "deciding") -> dict[str, Any] | None:
        """One decision. The manager may use its own tools first (read, check, fix) before it answers."""
        hw = hardware_brief(self.store) if self.can_use_hardware else ""
        hw_section = f"# Hardware status (you own this)\n{hw}\n\n" if hw else ""
        rules = project_rules(self.cfg)
        rules_section = (f"# Project rules (mandatory; apply them to every worker ticket you write)\n{rules}\n\n"
                         if rules else "")
        box = self._toolbox(ticket_id)
        system = (f"{self.role_text}\n\n{rules_section}# Your memory\n{self.memory.resume_brief()}\n\n"
                  f"{hw_section}{MANAGER_PROTOCOL}\n\n{box.describe()}")
        pol = self.cfg["policies"]
        limit, max_tools = pol["capability_retry_limit"], int(pol.get("max_manager_tool_steps", 25))
        msgs: list[dict[str, Any]] = [{"role": "user", "content": user}]
        invalid = tool_steps = 0
        while True:
            model = self.manager_models[min(invalid, len(self.manager_models) - 1)]
            if self.store.get_ticket(ticket_id)["status"] == "cancelled":
                raise _Cancelled(ticket_id)
            self._status(ticket_id, "thinking", doing, model)
            c = self.provider.complete(model=model, system=system, messages=msgs)
            self.store.record_usage(ticket_id, self.name, self.department, c, self._agent_id(ticket_id))
            if self.store.get_ticket(ticket_id)["status"] == "cancelled":
                raise _Cancelled(ticket_id)
            if c.refused:
                return {"action": "escalate", "kind": "refusal", "reason": f"model refusal ({c.refusal_category})"}
            reply = parse_reply(c.text, allow_dsml=True)
            if not reply:
                invalid += 1
                if invalid >= limit:
                    return None
                msgs = msgs + [{"role": "assistant", "content": c.text or "(empty)"},
                               {"role": "user", "content": "Invalid reply. Respond with exactly one JSON object."}]
                continue
            invalid = 0
            if reply.get("action") != "tool":
                return reply
            tool_steps += 1
            name = str(reply.get("tool", ""))
            if tool_steps > max_tools:
                if tool_steps > max_tools + 2:
                    return None
                result: dict[str, Any] = {"ok": False, "error": "tool step limit reached; make your decision now"}
            else:
                self._status(ticket_id, "tool", f"{name} ({tool_steps}/{max_tools}) while {doing}")
                result = box.call(name, reply.get("args") or {})
                self.store.add_event(ticket_id, self.name, "tool_call", {"tool": name, "ok": result.get("ok")})
            msgs = compact(msgs + [{"role": "assistant", "content": c.text},
                                   {"role": "user", "content": "Tool result: " + json.dumps(result)[:8000]}])

    @property
    def code_repo(self) -> Any:
        if self._code_repo is False:
            self._code_repo = code_repo_from_cfg(self.cfg)
        return self._code_repo

    # ------------------------------------------------------------------ workers
    def _code_briefing(self, worktree: Path, spec: CodeSpec) -> str:
        """The code ticket's limits and the context files, appended to the worker's first message."""
        out = ["", "", "## Code ticket",
               "You work in your own git worktree. Your scope (allowed paths): " + ", ".join(spec.allowed_paths),
               "Acceptance commands (all must pass; run_tests runs them): " + "; ".join(spec.acceptance)]
        budget = 24000
        for rel in spec.context_files:
            p = worktree / rel
            if p.is_file() and budget > 0:
                text = p.read_text(encoding="utf-8", errors="replace")[: min(6000, budget)]
                budget -= len(text)
                out += ["", f"### Context file: {rel}", "```", text, "```"]
        return chr(10).join(out)

    def _run_code_worker(self, c: dict[str, Any], models: list[str]) -> None:
        """One code ticket: isolated worktree, the engineer's tools, and automatic gates when the worker says done."""
        repo = self.code_repo
        if repo is None:
            self.store.escalate(c["id"], c["assignee"], "code tickets need a git repository (set code.repo in the config)",
                                kind="technical", to="top_manager")
            return
        spec, code_cfg = CodeSpec.from_ticket(c), self.cfg.get("code") or {}
        worktree = repo.ensure_worktree(c["id"])
        self.store.add_event(c["id"], c["assignee"], "code_started", {"worktree": str(worktree), "branch": repo.branch(c["id"])})
        box = CodeToolBox(self.workspace, c["assignee"], root=worktree, spec=spec,
                          timeout=float(code_cfg.get("test_timeout", 600)), store=self.store, ticket_id=c["id"],
                          departments=self.departments)

        def gate_cb(result: dict[str, Any]) -> str | None:
            repo.commit_all(c["id"], f"{c['id']}: {c['title']}")
            gates = run_gates(repo, c["id"], spec, code_cfg)
            ok = all(g["ok"] for g in gates)
            self.store.add_event(c["id"], c["assignee"], "code_gates", {
                "ok": ok, "gates": [{"name": g["name"], "ok": g["ok"], "warn": g.get("warn", False),
                                     "detail": g["detail"][:800]} for g in gates]})
            if not ok:
                return format_gate_failures(gates)
            files = [p for _, p in repo.changed_files(c["id"])]
            result["code"] = {"branch": repo.branch(c["id"]), "files": files, "diffstat": repo.diffstat(c["id"]),
                              "outside_scope": sorted(set(files) - {f for f in files if any(
                                  glob_match(f, a) for a in spec.allowed_paths)})}
            self.store.add_event(c["id"], c["assignee"], "code_ready", {"branch": repo.branch(c["id"]), "files": files})
            return None

        run_ticket(self.store, self.provider, c["id"], c["assignee"], self._with_rules(load_role(c["worker_role"])), models,
                   self.cfg["policies"]["capability_retry_limit"], toolbox=box, on_done=gate_cb,
                   extra_context=self._code_briefing(worktree, spec), department=self.department,
                   max_tool_steps=int(self.cfg["policies"].get("max_tool_steps", 40)))

    def _run_child(self, c: dict[str, Any], models: list[str]) -> None:
        """Run one worker (in a pool thread). A crash in one worker never takes the department down."""
        try:
            if c.get("code") is not None:
                self._run_code_worker(c, models)
                return
            box = ToolBox(self.workspace, actor=c["assignee"], can_acquire=self.can_acquire_tools,
                          store=self.store, ticket_id=c["id"], can_use_hardware=self.can_use_hardware,
                          jobs=self.jobs, read_roots=self.cfg.get("read_roots") or [], departments=self.departments)
            run_ticket(self.store, self.provider, c["id"], c["assignee"], self._with_rules(load_role(c["worker_role"])),
                       models, self.cfg["policies"]["capability_retry_limit"], toolbox=box, department=self.department,
                       max_tool_steps=int(self.cfg["policies"].get("max_tool_steps", 40)))
        except Halted:
            raise
        except Exception as e:
            note = f"worker crashed: {type(e).__name__}: {e}"[:400]
            self.store.add_event(c["id"], self.name, "run_error", {"error": note})
            if self.store.get_ticket(c["id"])["status"] in ("open", "in_progress"):
                self.store.escalate(c["id"], self.name, note, kind="technical", to="manager")

    def _models_for(self, c: dict[str, Any], index: int) -> list[str]:
        models = self.worker_models
        if self.diverse_workers and len(models) > 1:      # spread verifiers across models
            k = index % len(models)
            return models[k:] + models[:k]
        used = actions.retries_used(self.store, c["id"])   # each retry moves to the next fallback model
        return models[min(used, len(models) - 1):]

    def _with_rules(self, role_text: str) -> str:
        """A worker's role prompt plus the project's mandatory rules."""
        rules = project_rules(self.cfg)
        return f"{role_text}\n\n# Project rules (mandatory; breaking one makes the work unacceptable)\n{rules}" if rules else role_text

    # ---------------------------------------------------------------- reviewing
    def _children_summary(self, ticket_id: str) -> str:
        """One entry per worker. Failed or escalated workers include why, a sample of their last bad reply, and how
        many retries they have used; follow-ups the workers raised are listed so nothing they noticed is lost."""
        max_retries = self.cfg["policies"]["max_worker_retries"]
        lines = []
        for ch in self.store.list_tickets(parent_id=ticket_id):
            res = ch["result"] or {}
            state = branch_state(self.store, ch["id"]) if ch.get("code") is not None else None
            tag = ch["status"] + (f", branch {state}" if state else "")
            line = f"- [{tag}] {ch['id']} {ch['title']} -> {json.dumps(res)[:700] if ch['result'] is not None else '-'}"
            if res.get("follow_ups"):
                line += "\n    follow-ups raised: " + "; ".join(str(f) for f in res["follow_ups"])[:600]
            if ch["status"] in ("escalated", "failed"):
                events = self.store.events(ch["id"])
                line += f"\n    reason: {actions.why_from_events(events) or '(none recorded)'}"
                bad = [e for e in events if e["type"] == "invalid_reply"]
                if bad:
                    p = bad[-1]["payload"]
                    line += (f"\n    its last invalid reply ({p['chars']} chars) began: {p['head'][:200]!r}"
                             f" ... and ended: {p['tail'][-120:]!r}")
                line += f"\n    retries used: {actions.retries_used(self.store, ch['id'])}/{max_retries}"
                if ch.get("code") is not None:
                    line += ("\n    its worktree keeps the partial work: retry_workers continues it there, or fix it "
                             "yourself with the branch tools")
            lines.append(line)
        return "\n".join(lines) or "(no worker tickets)"

    def _pending_branches(self, ticket_id: str) -> list[dict[str, Any]]:
        if not self.review_by_manager:
            return []
        return [c for c in self.store.list_tickets(parent_id=ticket_id)
                if c.get("code") is not None and c["status"] == "done" and branch_state(self.store, c["id"]) == "ready"]

    def _branch_packets(self, ticket_id: str, only: list[dict[str, Any]] | None = None) -> str:
        """Review packets for branches awaiting this manager: summary, open questions, gates, scope, the diff."""
        ready = only if only is not None else self._pending_branches(ticket_id)
        if not ready or self.code_repo is None:
            return ""
        parts = ["", "", "# Branches awaiting YOUR review (you own the repository; workers cannot merge)"]
        for c in ready:
            res = c.get("result") or {}
            gates = [e for e in self.store.events(c["id"]) if e["type"] == "code_gates"]
            warn = [g["detail"] for g in (gates[-1]["payload"]["gates"] if gates else []) if g.get("warn")]
            try:
                diff = self.code_repo.diff(c["id"], 150)
            except CodeError as e:
                diff = f"(diff unavailable: {e})"
            parts += [f"## {c['id']}: {c['title']}",
                      f"worker summary: {res.get('summary', '-')}",
                      f"open questions: {res.get('open_questions') or 'none'}",
                      f"scope notes: {res.get('scope_notes') or '-'}" + (f"  WARNING: {'; '.join(warn)}" if warn else ""),
                      f"files: {', '.join((res.get('code') or {}).get('files', []))}; all automatic checks passed",
                      "```diff", diff, "```"]
        parts += ["Review like a tech lead: read open questions first (the usual source of subtle bugs), then the diff "
                  "for design and correctness against the ticket. Accept what you would build on; fix small problems "
                  "yourself with the branch tools and then accept; reject the rest with feedback that says what is wrong, "
                  "where, and what right looks like. Reply with the integrate action."]
        return "\n".join(parts)

    def _integrate(self, ticket_id: str, d: dict[str, Any]) -> None:
        """Apply the manager's review: merge accepted branches into the integration branch (re-checked if the manager
        edited them), send rejected ones back as rework tickets carrying the feedback."""
        repo, code_cfg = self.code_repo, self.cfg.get("code") or {}
        kids = {c["id"]: c for c in self.store.list_tickets(parent_id=ticket_id)}
        errors, merged, reworked = [], [], []

        def ready(cid: str) -> dict[str, Any] | None:
            c = kids.get(cid)
            return c if c and c.get("code") is not None and branch_state(self.store, cid) == "ready" else None

        for cid in [str(x) for x in d.get("accept") or []]:
            c = ready(cid)
            if c is None:
                errors.append(f"{cid}: not one of your branches awaiting review")
                continue
            try:
                self._status(ticket_id, "integrating", f"merging {cid}")
                if repo.has_worktree(cid) and repo.commit_all(cid, f"{self.name}: review fixes"):
                    gates = run_gates(repo, cid, CodeSpec.from_ticket(c), code_cfg)    # your edits are checked too
                    if not all(g["ok"] for g in gates):
                        errors.append(f"{cid}: your fixes broke its checks: {format_gate_failures(gates)[:600]}")
                        continue
                out = repo.merge_to_integration(cid, f"Merge {cid}: {c['title']}", list(code_cfg.get("integration_checks") or []),
                                                float(code_cfg.get("test_timeout", 600)))
            except CodeError as e:
                errors.append(f"{cid}: {e}")
                continue
            self.store.add_event(cid, self.name, "code_accepted",
                                 {"merge_commit": out["merge_commit"], "by": self.name, "into": repo.integration_branch})
            merged.append(cid)
        for item in d.get("reject") or []:
            cid, feedback = str(item.get("ticket_id", "")), str(item.get("feedback", "")).strip()
            c = ready(cid)
            if c is None or not feedback:
                errors.append(f"{cid}: " + ("not one of your branches awaiting review" if c is None else "reject needs feedback"))
                continue
            spec = rework_spec(c, feedback)
            role = spec["worker_role"]
            n = 1 + sum(1 for k in self.store.list_tickets(parent_id=ticket_id) if k["worker_role"] == role)
            new = self.store.create_ticket({**spec, "assignee": f"worker:{role}-{n}"}, actor=self.name, parent_id=ticket_id)
            repo.discard(cid)
            self.store.add_event(cid, self.name, "code_rejected", {"feedback": feedback[:500], "replacement": new["id"]})
            reworked.append(f"{cid} -> {new['id']}")
        self.memory.append_journal(f"[{ticket_id}] integrated {merged or 'nothing'}; rework {reworked or 'none'}"
                                   + (f"; problems: {'; '.join(errors)}" if errors else "")
                                   + (f". {d['notes']}" if d.get("notes") else ""))
        if errors:
            self._notice[ticket_id] = ("Some of your integrate decisions could not be applied:\n- " + "\n- ".join(errors)
                                       + "\nA conflict or failing check means the branch must be redone on top of the current "
                                         "integration branch: reject it with feedback, or fix it yourself with the branch tools.")

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
                errors.append(f"{wid}: retry limit ({max_retries}) reached; delegate different work, fix it yourself, or give up on it")
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
        for spec in d.get("tickets", []):          # validate every code spec first, so a bad one creates nothing
            if isinstance(spec, dict) and spec.get("code") is not None:
                if not self.writes_code:
                    raise StoreError("only the Software department writes code: use request_work to give it this task")
                problems = CodeSpec.problems(spec["code"])
                if problems:
                    raise StoreError(f"code ticket {spec.get('title', '?')!r} is not usable: " + "; ".join(problems))
                if self.code_repo is None:
                    raise StoreError("code tickets need a git repository (set code.repo in the config)")
        for spec in d.get("tickets", []):
            spec = dict(spec)
            if spec.get("code") is not None:
                spec.setdefault("worker_role", "code_worker")
            role = re.sub(r"[^a-z0-9]+", "_", str(spec.get("worker_role") or "worker").lower()).strip("_") or "worker"
            spec["worker_role"] = role
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
            self._status(ticket_id, "verifying", "independent verifiers are checking the result")
            report = self.verifier.verify(t, d.get("result"), self.name)
            verdict = report["verdict"]
            if verdict == "pass":
                break
            left = max_rounds - len(past) - 1
            d2 = self._decide(
                f"# Your ticket\n{ticket_prompt(t)}\n\n# Verification report (independent, advisory)\n"
                f"{self.verifier.format_report(report)}\n\nWeigh this heavily; it is written by checkers with the "
                f"broadest view of the goal. You keep the final say. Fix the problems (yourself, or delegate follow-ups), "
                f"or finish with 'override_reason' if you disagree, or escalate. Verification rounds left: {left}.",
                ticket_id, "answering verification")
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
    def work(self, ticket_id: str, max_rounds: int | None = None) -> dict[str, Any]:
        """Drive one ticket to an outcome, like a team lead: every runnable worker runs in parallel; questions are
        answered and finished branches reviewed as soon as they arrive (not after everyone is done); the manager only
        plans or concludes when nothing else is pending. Returns the settled ticket (or one waiting on someone above)."""
        try:
            return self._work(ticket_id, max_rounds)
        except _Cancelled:                    # cancelled from above mid-decision; workers stop at their next call
            return self._settle(ticket_id)

    def _work(self, ticket_id: str, max_rounds: int | None = None) -> dict[str, Any]:
        max_rounds = max_rounds or int(self.cfg["policies"].get("max_manager_rounds", 20))
        t = self.store.get_ticket(ticket_id)
        if t["status"] == "open":
            self.store.transition(ticket_id, "in_progress", self.name, note="manager picked up")
        running: dict[str, Future] = {}
        deferred: set[str] = set()                 # branches the manager chose to decide on at the final review
        rounds = launched = 0
        while True:
            t = self.store.get_ticket(ticket_id)
            if t["status"] != "in_progress":
                break
            children = self.store.list_tickets(parent_id=ticket_id)

            # 1. every runnable worker runs, in parallel ('in_progress' ones not running are orphans or retries)
            for c in children:
                if c["status"] in ("open", "in_progress") and c["id"] not in running:
                    running[c["id"]] = self.pool.submit(self._run_child, c, self._models_for(c, launched))
                    launched += 1

            # 2. answer questions as soon as they arrive
            asking = [c for c in children if c["status"] == "needs_clarification"]
            if asking:
                rounds += 1
                for c in asking:
                    q = next(q for q in c["questions"] if "answer" not in q)
                    d = self._decide(f"{ticket_prompt(t)}\n\nA worker on ticket {c['id']} asks: {q['question']}\n"
                                     "Answer it (look things up with your tools if needed) with the answer action.",
                                     ticket_id, f"answering {c['id']}")
                    if d and d.get("action") == "answer":
                        self.store.answer_question(c["id"], self.name, str(d.get("answer", "")))
                    elif d and self._apply(ticket_id, d):
                        return self._settle(ticket_id)          # asked/escalated upward instead
                    else:     # the manager is the one stuck, so this goes to the top manager
                        self.store.escalate(c["id"], self.name, "manager could not answer the worker's question",
                                            kind="blocked", to="top_manager")
                continue

            # 3. review finished branches while others still work, so nobody waits on the slowest worker
            ready = [c for c in self._pending_branches(ticket_id) if c["id"] not in deferred]
            if ready and running:
                rounds += 1
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\n# Worker results so far\n"
                                 f"{self._children_summary(ticket_id)}{self._branch_packets(ticket_id, ready)}\n\n"
                                 f"{len(running)} worker(s) are still running. Review the branches above now with the "
                                 "integrate action.", ticket_id, f"reviewing {len(ready)} branch(es)")
                if d and d.get("action") == "integrate":
                    self._integrate(ticket_id, d)
                else:
                    deferred |= {c["id"] for c in ready}
                continue

            # 4. wait for the next worker to finish
            if running:
                self._status(ticket_id, "waiting", f"{len(running)} worker(s) running")
                wait(list(running.values()), return_when=FIRST_COMPLETED)
                for cid in [k for k, f in running.items() if f.done()]:
                    running.pop(cid).result()            # re-raises Halted (pause/budget); other errors were handled
                continue

            # 5. nothing pending: plan the work, or review everything and decide
            if rounds >= max_rounds:
                self.store.escalate(ticket_id, self.name, f"no conclusion after {max_rounds} decision rounds",
                                    kind="technical")
                break
            rounds += 1
            if not children and t.get("worker_tickets"):
                # the top manager wrote exact worker tickets: delegate them as written, no planning call
                try:
                    self._delegate(ticket_id, {"tickets": t["worker_tickets"]})
                except StoreError as e:
                    self.store.escalate(ticket_id, self.name, f"worker_tickets rejected: {e}", kind="blocked")
                    break
                self.memory.append_journal(f"[{ticket_id}] delegated {len(t['worker_tickets'])} worker ticket(s) as written")
                continue
            if not children:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\nPlan the work. Independent pieces run in parallel, "
                                 "so split work that can proceed side by side into separate worker tickets.",
                                 ticket_id, "planning")
            else:
                d = self._decide(f"# Your ticket\n{ticket_prompt(t)}\n\n# Worker results\n"
                                 f"{self._children_summary(ticket_id)}\n\nReview, then finish or delegate follow-ups. "
                                 "Before finishing, make sure there are no holes: every acceptance criterion of your ticket "
                                 "is met by real evidence, and every follow-up your workers raised is either done, delegated, "
                                 "or listed in your result's follow_ups for the top manager."
                                 + self._branch_packets(ticket_id)
                                 + (f"\n\n# IMPORTANT\n{self._notice.pop(ticket_id)}" if ticket_id in self._notice else ""),
                                 ticket_id, "reviewing results")
            if d is None:
                self.store.escalate(ticket_id, self.name, "no valid decision from the manager model", kind="technical")
                break
            if d.get("action") == "integrate":
                self._integrate(ticket_id, d)
                continue
            if d.get("action") == "done" and self._pending_branches(ticket_id):
                waiting = ", ".join(c["id"] for c in self._pending_branches(ticket_id))
                self._notice[ticket_id] = (f"You cannot finish yet: these worker branches are still awaiting YOUR review: {waiting}. "
                                           "You own the repository: integrate (accept) each good one or reject it with feedback, then finish.")
                continue
            if d.get("action") == "retry_workers":
                applied, errors = self._retry_workers(ticket_id, d)
                self.memory.append_journal(f"[{ticket_id}] retried {applied} worker(s)"
                                           + (f"; problems: {'; '.join(errors)}" if errors else "")
                                           + (f". {d['notes']}" if d.get("notes") else ""))
                if applied:
                    continue                  # step 1 re-runs the reopened workers next round
                self._notice[ticket_id] = "Your retries could not be applied: " + ("; ".join(errors) or "none given")
                continue
            if d.get("action") == "done":
                d = self._verify_gate(ticket_id, d)
            if d.get("action") == "delegate":
                try:
                    self._delegate(ticket_id, d)
                except StoreError as e:      # e.g. the per-manager worker cap: tell the manager instead of crashing
                    self._notice[ticket_id] = (f"Your last delegation was rejected: {e}. Do the remaining work yourself "
                                               "with your tools, finish with the results you have, or retry_workers on an "
                                               "escalated one.")
                    self.memory.append_journal(f"[{ticket_id}] delegation rejected: {e}")
                    continue
                if d.get("notes"):
                    self.memory.append_journal(f"[{ticket_id}] {d['notes']}")
                continue
            if not self._apply(ticket_id, d):
                self.store.escalate(ticket_id, self.name, f"unknown action {d.get('action')!r} from the manager model",
                                    kind="technical")
            break
        return self._settle(ticket_id)

    def _settle(self, ticket_id: str) -> dict[str, Any]:
        t = self.store.get_ticket(ticket_id)
        reason = (t.get("escalation") or {}).get("reason", "")
        self._status(ticket_id, {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(t["status"], t["status"]),
                     reason or t["title"][:120])
        return t
