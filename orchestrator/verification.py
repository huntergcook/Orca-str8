"""Independent, advisory verification of a manager's result against the broader goal.

Each verifier is a fresh stateless model call (no shared context with the work it checks).
Different models are used for diversity. The report goes back to the department manager,
who weighs it heavily but keeps the final say; overrides are logged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent import parse_reply, ticket_prompt
from .hardware import resolve_in_workspace
from .providers import Provider
from .store import Store, StoreError

ORDER = {"pass": 0, "concerns": 1, "fail": 2}

STRICTNESS = {
    "quick_and_dirty": "Rigor is QUICK-AND-DIRTY: only flag problems that make the result wrong, unsafe, or "
                       "misleading for the stated goal. Ignore polish, missing edge cases and style.",
    "balanced": "Rigor is BALANCED: flag correctness errors, unmet acceptance criteria, claims without support, "
                "and drift from the broader goal.",
    "rigorous": "Rigor is RIGOROUS: be exhaustive and strict. Every claim needs evidence you can see; flag "
                "anything unverified, unreproducible, or inconsistent with the broader goal.",
}

SYSTEM = """\
You are an independent verifier. You did not do this work and you have no stake in it. Compare the
claimed result against the ticket's acceptance criteria AND the broader project goal. Check the evidence
provided; do not take claims on faith. {strictness}

Reply with exactly one JSON object:
  {{"verdict": "pass" | "concerns" | "fail",
    "issues": ["specific problem, citing what you saw", ...],
    "advice": "what the manager should do to fix or confirm"}}
"pass" means no issues you would act on at this rigor. "fail" means the result should not be accepted as is."""


class Verifier:
    def __init__(self, store: Store, provider: Provider, cfg: dict[str, Any],
                 workspace: str | Path, models: list[str]) -> None:
        self.store, self.provider, self.cfg = store, provider, cfg
        self.workspace = Path(workspace)
        self.models = models or []

    # --------------------------------------------------------------- context

    def root_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        t = ticket
        while t.get("parent_id"):
            t = self.store.get_ticket(t["parent_id"])
        return t

    def scope_text(self, ticket: dict[str, Any]) -> str:
        root = self.root_ticket(ticket)
        state = self.workspace / "memory" / "top_manager" / "state.md"
        notes = state.read_text(encoding="utf-8")[:4000] if state.exists() else "(none)"
        return (f"## Broader goal (top-level ticket {root['id']}: {root['title']})\n{root['context']}\n"
                f"Acceptance: {'; '.join(root['acceptance_criteria'])}\n\n"
                f"## Top manager's state notes (goal, stopping criteria, rigor profile)\n{notes}")

    def evidence(self, ticket: dict[str, Any], result: Any, max_files: int = 5) -> str:
        """Artifacts the result points at: files named in return_spec.destination, workspace_refs,
        or any string value in the result that is an existing workspace file."""
        candidates: list[str] = list(ticket.get("workspace_refs", []))
        dest = ticket["return_spec"].get("destination")
        if dest:
            candidates.append(dest)

        def walk(x: Any) -> None:
            if isinstance(x, str):
                candidates.append(x)
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(result)
        seen, parts = set(), []
        for c in candidates:
            if len(parts) >= max_files or len(c) > 300 or "\n" in c:
                continue
            try:
                p = resolve_in_workspace(self.workspace, c)
            except StoreError:
                continue
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            parts.append(f"### {c}\n{p.read_text(encoding='utf-8', errors='replace')[:6000]}")
        return "\n\n".join(parts) or "(no artifact files found; judge from the result text alone and say so)"

    def children_text(self, ticket: dict[str, Any]) -> str:
        rows = [f"- [{c['status']}] {c['title']} -> {json.dumps(c['result'])[:600]}"
                for c in self.store.list_tickets(parent_id=ticket["id"])]
        return "\n".join(rows) or "(manager did the work directly)"

    # ----------------------------------------------------------------- verify

    def verify(self, ticket: dict[str, Any], result: Any, actor: str) -> dict[str, Any]:
        user = (f"{self.scope_text(ticket)}\n\n## Ticket under review\n{ticket_prompt(ticket)}\n\n"
                f"## Worker results\n{self.children_text(ticket)}\n\n"
                f"## Claimed final result\n{json.dumps(result)[:6000]}\n\n"
                f"## Evidence files\n{self.evidence(ticket, result)}")
        system = SYSTEM.format(strictness=STRICTNESS.get(ticket.get("rigor", "balanced"), STRICTNESS["balanced"]))
        reports = []
        for model in self.models:
            reports.append(self._one(model, system, user, ticket["id"]))
        worst = max((r["verdict"] for r in reports), key=ORDER.get, default="pass")
        report = {"verdict": worst, "reports": reports}
        self.store.add_verification(ticket["id"], actor, worst, [self._note(r) for r in reports])
        return report

    def _one(self, model: str, system: str, user: str, ticket_id: str) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for _ in range(2):
            c = self.provider.complete(model=model, system=system, messages=msgs)
            self.store.add_event(ticket_id, "verifier", "usage", {
                "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})
            if c.refused:
                return {"model": model, "verdict": "concerns", "issues": [f"verifier model declined ({c.refusal_category})"],
                        "advice": "this check could not run on this model"}
            r = parse_reply(c.text, key="verdict")
            if r and r.get("verdict") in ORDER:
                return {"model": model, "verdict": r["verdict"], "issues": [str(i) for i in r.get("issues", [])],
                        "advice": str(r.get("advice", ""))}
            msgs = msgs + [{"role": "assistant", "content": c.text or "(empty)"},
                           {"role": "user", "content": "Invalid reply. Respond with exactly one JSON object."}]
        return {"model": model, "verdict": "concerns", "issues": ["verifier returned no valid verdict"], "advice": ""}

    @staticmethod
    def _note(r: dict[str, Any]) -> str:
        issues = "; ".join(r["issues"]) or "none"
        return f"[{r['model']}] {r['verdict']}: {issues}" + (f" | advice: {r['advice']}" if r["advice"] else "")

    @staticmethod
    def format_report(report: dict[str, Any]) -> str:
        lines = [f"Overall verdict: {report['verdict'].upper()}"]
        for r in report["reports"]:
            lines.append(f"- verifier {r['model']}: {r['verdict']}")
            lines += [f"    issue: {i}" for i in r["issues"]]
            if r["advice"]:
                lines.append(f"    advice: {r['advice']}")
        return "\n".join(lines)
