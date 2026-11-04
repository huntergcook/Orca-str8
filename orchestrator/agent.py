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

from .dsml import has_dsml, parse_dsml
from .providers import Completion, Provider
from .store import Store

PROTOCOL = """\
Work only on the ticket below; stay inside its scope. Reply with exactly one JSON object:
  {"action": "done", "result": <shaped as the ticket's return_spec>}
  {"action": "ask", "question": "<what you need answered>"}
  {"action": "escalate", "kind": "safety" | "blocked", "reason": "<what is wrong>"}
      ("safety": anything physically or otherwise unsafe; goes straight to the human. "blocked": you cannot
       finish and need your manager's help, which is the normal case.)
  {"action": "fail", "reason": "<why it cannot be done>"}
Reply with plain JSON text only: never use XML-style or special-token tool-call markup (anything like
<|...|> or DSML tags). Tool calls are JSON objects too.
Keep the JSON reply SMALL. Put long content (reports, notes, data, code) in a file with write_file and
return only a short summary and the file path in "result". Never put a long document inside the JSON.
Match your effort to the ticket's rigor level."""


def as_result(value: Any) -> dict[str, Any]:
    """Tickets store results as JSON objects. Models sometimes return a bare string (e.g. a whole markdown
    report) or a list; wrap it instead of failing the ticket and losing the work."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    return {"text": value} if isinstance(value, str) else {"value": value}


def parse_reply(text: str, key: str = "action", allow_dsml: bool = False) -> dict[str, Any] | None:
    """Extract the first balanced JSON object that has `key` (default 'action'). With allow_dsml, a DeepSeek
    native tool call (see dsml.py) is accepted as a fallback and returned as an ordinary tool action."""
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
                        # strict=False tolerates raw newlines/tabs inside strings, which models often emit
                        obj = json.loads(text[start:i + 1], strict=False)
                        if isinstance(obj, dict) and key in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    if allow_dsml and key == "action" and has_dsml(text):
        return parse_dsml(text)
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
               models: list[str], capability_retry_limit: int = 3, toolbox: Any | None = None,
               max_tool_steps: int = 12) -> dict[str, Any]:
    """Run one worker to completion on a ticket and return the updated ticket.

    Capability failures (unparseable reply) retry along `models` up to the limit, then escalate.
    Safety refusals retry ONCE with an honest restatement on the SAME model, then escalate to the
    human with the refusal attached. They are never rerouted to another model automatically.
    Tool calls (when a toolbox is given) do not count against the retry limit, but are capped.
    """
    t = store.get_ticket(ticket_id)
    if t["status"] == "open":
        store.transition(ticket_id, "in_progress", actor, note="worker started")
        t = store.get_ticket(ticket_id)
    sep = chr(10) * 2
    interrupted = False
    for e in reversed(store.events(ticket_id)):
        if e["type"] == "worker_started":
            interrupted = True          # a worker began and never reached an outcome: crash or power loss
            break
        if e["type"] in ("question_asked", "retry") or (
                e["type"] == "status_changed" and e["payload"].get("to") in
                ("done", "failed", "cancelled", "escalated", "needs_clarification")):
            break
    store.add_event(ticket_id, actor, "worker_started", {"resumed_after_interruption": interrupted})
    system = role_text + sep + PROTOCOL + ((sep + toolbox.describe()) if toolbox else "")
    first = ticket_prompt(t)
    if interrupted:
        first += (sep + "NOTE: an earlier attempt at this ticket was interrupted (crash or power loss) and its "
                  "conversation was lost. Files it wrote under the workspace (for example work/" + ticket_id +
                  "/ or paths mentioned above) and any background jobs may still exist: look before redoing "
                  "work, and continue from where it left off.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
    refusals = invalid = tool_steps = 0

    # `invalid` counts CONSECUTIVE unparseable replies (reset by any valid reply, e.g. a tool call), so a
    # long tool-using conversation is not penalized for old mistakes. Every loop turn either returns,
    # grows a bounded counter (invalid, refusals, tool_steps) or breaks, so this always terminates.
    while True:
        # Refusal retries stay on the same model; only capability failures advance the chain.
        model = models[min(invalid, len(models) - 1)]
        c = provider.complete(model=model, system=system, messages=messages)
        _usage(store, ticket_id, actor, c)

        if c.refused:
            refusals += 1
            if refusals >= 2:
                store.add_event(ticket_id, actor, "refusal_escalated",
                                {"category": c.refusal_category, "model": c.model})
                return store.escalate(ticket_id, actor, f"model refusal ({c.refusal_category}); needs human decision",
                                      kind="refusal")
            store.add_event(ticket_id, actor, "refusal", {"category": c.refusal_category, "model": c.model})
            messages = messages[:1] + [{"role": "user", "content": _restated(t)}]
            continue

        reply = parse_reply(c.text, allow_dsml=True)
        if reply is not None and "DSML" in c.text and not c.text.lstrip().startswith("{"):
            store.add_event(ticket_id, actor, "reply_normalized", {"from": "dsml", "tool": reply.get("tool")})
        if reply is None:
            invalid += 1
            # Keep a sample of the bad reply so failures can be diagnosed afterwards.
            store.add_event(ticket_id, actor, "invalid_reply", {
                "n": invalid, "model": c.model, "chars": len(c.text), "head": c.text[:300], "tail": c.text[-200:]})
            if invalid >= capability_retry_limit:
                break
            messages = messages + [
                {"role": "assistant", "content": c.text or "(empty)"},
                {"role": "user", "content": "That was not a valid reply. Respond with exactly one JSON object "
                                            "per the protocol, keeping it short (long content goes in a file)."},
            ]
            continue
        invalid = 0

        action = reply["action"]
        if action == "tool" and toolbox is not None:
            tool_steps += 1
            if tool_steps > max_tool_steps:
                result: dict[str, Any] = {"ok": False, "error": "tool step limit reached; finish with done/ask/escalate/fail"}
            else:
                result = toolbox.call(str(reply.get("tool", "")), reply.get("args") or {})
                store.add_event(ticket_id, actor, "tool_call", {"tool": reply.get("tool"), "ok": result.get("ok")})
            messages = messages + [{"role": "assistant", "content": c.text},
                                   {"role": "user", "content": "Tool result: " + json.dumps(result)[:8000]}]
            if tool_steps > max_tool_steps + 2:
                break
            continue
        if action == "done":
            return store.transition(ticket_id, "done", actor, result=as_result(reply.get("result")))
        if action == "ask":
            return store.ask_question(ticket_id, actor, str(reply.get("question", "")))
        if action == "escalate":
            kind = reply.get("kind") if reply.get("kind") in ("safety", "blocked") else "blocked"
            return store.escalate(ticket_id, actor, str(reply.get("reason", "")), kind=kind)
        if action == "fail":
            return store.transition(ticket_id, "failed", actor, note=str(reply.get("reason", "")))
        break

    return store.escalate(ticket_id, actor, "could not obtain a valid reply within retry limit", kind="technical")
