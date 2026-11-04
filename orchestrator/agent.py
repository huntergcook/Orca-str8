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
  {"action": "done", "result": <shaped as the ticket's return_spec, plus "follow_ups": [...] (see SPEAK UP)>}
  {"action": "ask", "question": "<what you need answered>"}
  {"action": "escalate", "kind": "safety" | "blocked", "reason": "<what is wrong>"}
      ("safety": anything physically or otherwise unsafe; goes straight to the human. "blocked": you cannot
       finish and need your manager's help, which is the normal case.)
  {"action": "fail", "reason": "<why it cannot be done>"}
Reply with plain JSON text only: never use XML-style or special-token tool-call markup (anything like
<|...|> or DSML tags). Tool calls are JSON objects too.
Keep the JSON reply SMALL. Put long content (reports, notes, data, code) in a file with write_file and
return only a short summary and the file path in "result". Never put a long document inside the JSON.
Match your effort to the ticket's rigor level.
SPEAK UP: if you notice other work that should be done for the goal (a bug elsewhere, a missing test, a wrong
or stale document, a risk, a better approach), list it in result.follow_ups, one line each. You are expected
to, not just allowed to: nobody else may have seen it."""


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


SHORTENED = " ...[older tool result shortened to save context; read it again if you still need it]"


def compact(messages: list[dict[str, Any]], max_chars: int = 60000, keep_last: int = 4) -> list[dict[str, Any]]:
    """Keep a long tool-using conversation bounded: once it passes `max_chars`, shorten all but the newest tool
    results. Done in one sweep (not every turn) so the provider's prompt cache stays valid most of the time."""
    total = sum(len(m["content"]) for m in messages if isinstance(m.get("content"), str))
    if total <= max_chars:
        return messages
    idx = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m.get("content"), str)
           and m["content"].startswith("Tool result: ")]
    for i in idx[:-keep_last]:
        content = messages[i]["content"]
        if len(content) > 500 and not content.endswith(SHORTENED):
            messages[i] = {"role": "user", "content": content[:400] + SHORTENED}
    return messages


def run_ticket(store: Store, provider: Provider, ticket_id: str, actor: str, role_text: str,
               models: list[str], capability_retry_limit: int = 3, toolbox: Any | None = None,
               max_tool_steps: int = 40, on_done: Any | None = None, extra_context: str = "",
               max_done_rejects: int = 3, department: str | None = None, context_chars: int = 60000) -> dict[str, Any]:
    """Run one worker to completion on a ticket and return the updated ticket.

    Capability failures (unparseable reply) retry along `models` up to the limit, then escalate.
    Safety refusals retry ONCE with an honest restatement on the SAME model, then escalate to the
    human with the refusal attached. They are never rerouted to another model automatically.
    Tool calls do not count against the retry limit but are capped, with a countdown so the worker saves its work.
    The worker's live state is kept in the agents table for the dashboard; usage is attributed to `department`.
    """
    def status(state: str, detail: str = "", model: str | None = None) -> None:
        try:
            store.set_agent(ticket_id, actor, "worker", department, ticket_id, state, detail, model)
        except Exception:                 # status reporting must never break the work itself
            pass

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
    status("starting", t["title"][:120])
    system = role_text + sep + PROTOCOL + ((sep + toolbox.describe()) if toolbox else "")
    first = ticket_prompt(t) + extra_context
    if interrupted:
        first += (sep + "NOTE: an earlier attempt at this ticket was interrupted (crash, power loss or step limit) and "
                  "its conversation was lost. Files it wrote (in the workspace, e.g. work/" + ticket_id + "/, or in your "
                  "worktree) and any background jobs may still exist: look before redoing work, and continue from there.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
    refusals = invalid = tool_steps = done_rejects = calls = 0

    # `invalid` counts CONSECUTIVE unparseable replies (reset by any valid reply, e.g. a tool call), so a
    # long tool-using conversation is not penalized for old mistakes. Every loop turn either returns,
    # grows a bounded counter (invalid, refusals, tool_steps, done_rejects) or breaks, so this always terminates.
    while True:
        # Refusal retries stay on the same model; only capability failures advance the chain.
        model = models[min(invalid, len(models) - 1)]
        calls += 1
        status("thinking", f"model call {calls}", model)
        c = provider.complete(model=model, system=system, messages=messages)
        store.record_usage(ticket_id, actor, department, c, ticket_id)

        if c.refused:
            refusals += 1
            if refusals >= 2:
                store.add_event(ticket_id, actor, "refusal_escalated",
                                {"category": c.refusal_category, "model": c.model})
                status("escalated", f"model refusal ({c.refusal_category})")
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
            name = str(reply.get("tool", ""))
            if tool_steps > max_tool_steps:
                result: dict[str, Any] = {"ok": False, "error": "tool step limit reached; reply done now with what you "
                                                                "have, or escalate saying exactly what is left"}
            else:
                status("tool", f"{name} ({tool_steps}/{max_tool_steps})")
                result = toolbox.call(name, reply.get("args") or {})
                store.add_event(ticket_id, actor, "tool_call", {"tool": name, "ok": result.get("ok")})
            left = max_tool_steps - tool_steps
            note = ""
            if left in (8, 3):
                note = (f"\n[{left} tool steps left. Wrap up: save your work now (write the file, run the tests) and "
                        "reply done, or escalate saying exactly what is missing.]")
            elif left == 0:
                note = "\n[That was your last tool step. Reply done now with what you have, or escalate with what is left.]"
            messages = messages + [{"role": "assistant", "content": c.text},
                                   {"role": "user", "content": "Tool result: " + json.dumps(result)[:8000] + note}]
            messages = compact(messages, context_chars)
            if tool_steps > max_tool_steps + 2:
                status("escalated", "used all tool steps")
                return store.escalate(ticket_id, actor,
                                      f"used all {max_tool_steps} tool steps without finishing; its partial work is kept, "
                                      "so a retry continues from it", kind="technical")
            continue
        if action == "done":
            result = as_result(reply.get("result"))
            if on_done is not None:
                # e.g. code tickets: the orchestrator runs its gates now and, if they fail, the SAME
                # conversation gets the failures back to fix (no fresh worker, no lost context)
                status("checking", "the orchestrator is running the checks on this work")
                feedback = on_done(result)
                if feedback:
                    done_rejects += 1
                    store.add_event(ticket_id, actor, "done_rejected", {"n": done_rejects, "feedback": feedback[:600]})
                    if done_rejects >= max_done_rejects:
                        status("escalated", "checks still failing")
                        return store.escalate(ticket_id, actor,
                                              f"still failing its checks after {done_rejects} attempts: {feedback[:300]}",
                                              kind="technical")
                    messages = messages + [{"role": "assistant", "content": c.text}, {"role": "user", "content": feedback}]
                    continue
            status("done", str(result.get("summary") or result.get("report_path") or "")[:200])
            return store.transition(ticket_id, "done", actor, result=result)
        if action == "ask":
            status("asking", str(reply.get("question", ""))[:200])
            return store.ask_question(ticket_id, actor, str(reply.get("question", "")))
        if action == "escalate":
            kind = reply.get("kind") if reply.get("kind") in ("safety", "blocked") else "blocked"
            status("escalated", str(reply.get("reason", ""))[:200])
            return store.escalate(ticket_id, actor, str(reply.get("reason", "")), kind=kind)
        if action == "fail":
            status("failed", str(reply.get("reason", ""))[:200])
            return store.transition(ticket_id, "failed", actor, note=str(reply.get("reason", "")))
        break

    status("escalated", "no valid reply")
    return store.escalate(ticket_id, actor, "could not obtain a valid reply within retry limit", kind="technical")
