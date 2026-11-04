"""Worker runner: turns one ticket into one model conversation and writes the outcome back.

Reply protocol (JSON object, optionally in a ```json fence):
  {"action": "done",     "result": {...}}          finished; result shaped by return_spec
  {"action": "ask",      "question": "..."}        need clarification from above
  {"action": "escalate", "reason": "..."}          safety issue / out of scope
  {"action": "fail",     "reason": "..."}          cannot be done
"""
from __future__ import annotations

import json
from typing import Any

from .providers import Completion, Provider
from .store import Store

PROTOCOL = """\
Work only on the ticket below; stay inside its scope. Reply with exactly one JSON object:
  {"action": "done", "result": <shaped as the ticket's return_spec>}
  {"action": "ask", "question": "<what you need answered>"}
  {"action": "escalate", "reason": "<safety issue or out-of-scope problem>"}
  {"action": "fail", "reason": "<why it cannot be done>"}
Match your effort to the ticket's rigor level."""


def parse_reply(text: str, key: str = "action") -> dict[str, Any] | None:
    """Extract the first balanced JSON object that has `key` (default 'action')."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict) and key in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def ticket_prompt(t: dict[str, Any]) -> str:
    qa = [q for q in t.get("questions", []) if "answer" in q]
    parts = [
        f"# Ticket {t['id']}: {t['title']}",
        f"Rigor: {t.get('rigor', 'balanced')}",
        f"Origin trace: {' -> '.join(t['path_trace'])}",
        f"## Context\n{t['context']}",
        "## Acceptance criteria\n" + "\n".join(f"- {c}" for c in t["acceptance_criteria"]),
        f"## Return spec\n{json.dumps(t['return_spec'])}",
    ]
    if t.get("workspace_refs"):
        parts.append("## Workspace files\n" + "\n".join(f"- {r}" for r in t["workspace_refs"]))
    if qa:
        parts.append("## Clarifications so far\n" + "\n".join(f"Q: {q['question']}\nA: {q['answer']}" for q in qa))
    return "\n\n".join(parts)


def _restated(t: dict[str, Any]) -> str:
    return (
        "Your previous reply was declined. To be clear about the real situation: this ticket "
        f"comes from the operator's own project (origin: {' -> '.join(t['path_trace'])}), and the "
        f"context above is accurate: {t['context']}\n"
        "Please do the legitimate parts of the task. If something specific cannot be done, say exactly "
        "which part and reply with an 'ask' or 'escalate' action instead of guessing."
    )


def _usage(store: Store, ticket_id: str, actor: str, c: Completion) -> None:
    store.add_event(ticket_id, actor, "usage", {
        "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})


def run_ticket(store: Store, provider: Provider, ticket_id: str, actor: str, role_text: str,
               models: list[str], capability_retry_limit: int = 3) -> dict[str, Any]:
    """Run one worker to completion on a ticket and return the updated ticket.

    Capability failures (unparseable reply) retry along `models` up to the limit, then escalate.
    Safety refusals retry ONCE with an honest restatement on the SAME model, then escalate to the
    human with the refusal attached. They are never rerouted to another model automatically.
    """
    t = store.get_ticket(ticket_id)
    if t["status"] == "open":
        store.transition(ticket_id, "in_progress", actor, note="worker started")
        t = store.get_ticket(ticket_id)
    system = f"{role_text}\n\n{PROTOCOL}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": ticket_prompt(t)}]
    refusals = 0

    for attempt in range(capability_retry_limit + 1):  # +1 slot for the refusal retry
        # Refusal retries stay on the same model; only capability failures advance the chain.
        model = models[min(attempt - refusals, len(models) - 1)]
        c = provider.complete(model=model, system=system, messages=messages)
        _usage(store, ticket_id, actor, c)

        if c.refused:
            refusals += 1
            if refusals >= 2:
                store.add_event(ticket_id, actor, "refusal_escalated",
                                {"category": c.refusal_category, "model": c.model})
                return store.transition(ticket_id, "escalated", actor,
                                        note=f"model refusal ({c.refusal_category}); needs human decision")
            store.add_event(ticket_id, actor, "refusal", {"category": c.refusal_category, "model": c.model})
            messages = messages[:1] + [{"role": "user", "content": _restated(t)}]
            continue

        reply = parse_reply(c.text)
        if reply is None:
            if attempt - refusals >= capability_retry_limit - 1:
                break
            messages = messages + [
                {"role": "assistant", "content": c.text or "(empty)"},
                {"role": "user", "content": "That was not a valid reply. Respond with exactly one JSON object per the protocol."},
            ]
            continue

        action = reply["action"]
        if action == "done":
            return store.transition(ticket_id, "done", actor, result=reply.get("result") or {})
        if action == "ask":
            return store.ask_question(ticket_id, actor, str(reply.get("question", "")))
        if action == "escalate":
            return store.transition(ticket_id, "escalated", actor, note=str(reply.get("reason", "")))
        if action == "fail":
            return store.transition(ticket_id, "failed", actor, note=str(reply.get("reason", "")))
        break

    return store.transition(ticket_id, "escalated", actor,
                            note="could not obtain a valid reply within retry limit")
