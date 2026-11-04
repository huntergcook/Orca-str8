"""Human/top-manager actions on tickets that need attention. Shared by the CLI (ctl) and the dashboard
so both behave identically. Every action validates the ticket's state first.

  answer  a question asked upward (needs_clarification)
  retry   an escalated ticket: append guidance and send it back to work
  cancel  give up on a ticket that needs attention
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil

from .store import Store, StoreError

CANCELLABLE = ("escalated", "needs_clarification", "blocked")
FINISHED = ("done", "failed", "cancelled")


def why_from_events(events: list[dict[str, Any]]) -> str:
    """One-line reason a ticket needs attention, from its events (payloads already decoded)."""
    for e in reversed(events):
        p = e.get("payload") or {}
        if e["type"] == "run_error":
            return str(p.get("error", ""))
        if e["type"] == "refusal_escalated":
            return f"model refusal ({p.get('category')}); needs your decision"
        if e["type"] == "status_changed" and p.get("to") in ("escalated", "blocked") and p.get("note"):
            return str(p["note"])
        if e["type"] == "question_asked":
            return str(p.get("question", ""))
    return ""


def run_is_active(workspace: str | Path) -> bool:
    """True if a `ctl run` process is alive (its lock file holds a live pid)."""
    try:
        pid = int((Path(workspace) / "RUNNING").read_text())
    except (OSError, ValueError):
        return False
    return psutil.pid_exists(pid)


def spawn_run(project: str | Path, workspace: str | Path) -> None:
    """Start `ctl run` detached for the project (output appended to workspace/run.log)."""
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    log = open(ws / "run.log", "ab")
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        flags["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "orchestrator.ctl", "--project", str(project), "run"],
                     cwd=str(project), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)


def answer(store: Store, ticket_id: str, text: str, actor: str) -> dict[str, Any]:
    if not text.strip():
        raise StoreError("an answer needs some text")
    t = store.get_ticket(ticket_id)
    if t["status"] != "needs_clarification":
        raise StoreError(f"{ticket_id} is {t['status']}, not waiting for an answer")
    t = store.answer_question(ticket_id, actor, text.strip())
    return {"ticket_id": ticket_id, "status": t["status"]}


def _reopen_ancestors(store: Store, ticket: dict[str, Any], actor: str) -> None:
    """Retrying a worker only helps if the tickets above it can pick the work back up."""
    parent_id = ticket.get("parent_id")
    chain = []
    while parent_id:
        p = store.get_ticket(parent_id)
        chain.append(p)
        parent_id = p.get("parent_id")
    for p in chain:
        if p["status"] in FINISHED:
            raise StoreError(f"parent ticket {p['id']} is already {p['status']}; retry or recreate that one instead")
    for p in chain:
        if p["status"] == "escalated":
            store.transition(p["id"], "in_progress", actor, note="reopened because a worker below was retried")


def retry(store: Store, ticket_id: str, text: str, actor: str) -> dict[str, Any]:
    t = store.get_ticket(ticket_id)
    if t["status"] != "escalated":
        raise StoreError(f"{ticket_id} is {t['status']}; only escalated tickets can be retried")
    _reopen_ancestors(store, t, actor)
    if text.strip():
        store.append_context(ticket_id, actor, text.strip())
    t = store.transition(ticket_id, "in_progress", actor, note="retry with guidance" if text.strip() else "retry")
    return {"ticket_id": ticket_id, "status": t["status"]}


def cancel(store: Store, ticket_id: str, text: str, actor: str) -> dict[str, Any]:
    t = store.get_ticket(ticket_id)
    if t["status"] not in CANCELLABLE:
        raise StoreError(f"{ticket_id} is {t['status']}; nothing to cancel")
    t = store.transition(ticket_id, "cancelled", actor, note=text.strip() or "cancelled by human")
    return {"ticket_id": ticket_id, "status": t["status"]}


ACTIONS = {"answer": answer, "retry": retry, "cancel": cancel}
