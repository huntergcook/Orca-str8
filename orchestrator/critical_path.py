"""Where is the time going? Tickets may declare `depends_on` (ids of tickets that must be done first). From that the
harness computes the longest unfinished chain (the critical path) and tells the operator when the project is slow
because the work is serial, so the director can break the chain instead of everyone waiting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import TERMINAL, Store

ACTIVE_RUNNING = {"in_progress"}
STALLED = {"needs_clarification", "blocked", "escalated"}


def deps_met(store: Store, ticket: dict[str, Any]) -> bool:
    """True when every ticket this one depends on is done (a failed or cancelled dependency never satisfies it)."""
    for d in ticket.get("depends_on") or []:
        try:
            if store.get_ticket(d)["status"] != "done":
                return False
        except Exception:
            return False
    return True


def _minutes_since(ts: str | None, now: datetime) -> int:
    if not ts:
        return 0
    try:
        return int((now - datetime.fromisoformat(ts)).total_seconds() // 60)
    except ValueError:
        return 0


def analyze(store: Store, max_parallel: int = 8, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tops = {t["id"]: t for t in store.list_tickets() if t["parent_id"] is None}
    active = {i: t for i, t in tops.items() if t["status"] not in TERMINAL}
    state: dict[str, str] = {}
    for i, t in active.items():
        if t["status"] in STALLED:
            state[i] = "stalled"
        elif not deps_met(store, t):
            state[i] = "waiting"
        elif t["status"] in ACTIVE_RUNNING:
            state[i] = "running"
        else:
            state[i] = "ready"
    memo: dict[str, int] = {}

    def length(i: str, seen: frozenset[str] = frozenset()) -> int:
        if i in memo:
            return memo[i]
        if i in seen:                                   # a dependency cycle: never loop
            return 1
        best = 0
        for d in tops.get(i, {}).get("depends_on") or []:
            if d in active:
                best = max(best, length(d, seen | {i}))
        memo[i] = 1 + best
        return memo[i]

    chain: list[str] = []
    if active:
        tail = max(active, key=lambda i: length(i))
        cur: str | None = tail
        while cur:
            chain.append(cur)
            nxt = [d for d in tops[cur].get("depends_on") or [] if d in active]
            cur = max(nxt, key=lambda d: length(d)) if nxt else None
        chain.reverse()                                 # first thing to finish first
    running = [i for i, s in state.items() if s == "running"]
    ready = [i for i, s in state.items() if s == "ready"]
    workers_running = sum(1 for i in running for k in store.list_tickets(parent_id=i) if k["status"] == "in_progress")
    slots = max(1, max_parallel)
    serial = bool(active) and len(running) + len(ready) <= 1 and workers_running <= 1 and \
        not any(s == "stalled" for s in state.values())
    head = chain[0] if chain else None
    head_minutes = _minutes_since(tops[head]["created_at"], now) if head else 0
    if not active:
        msg = "nothing active"
    elif serial and len(chain) > 1:
        msg = (f"SERIAL: {len(chain)} tickets in a chain, one running at a time; head '{tops[head]['title'][:50]}' "
               f"open {head_minutes} min. Break the chain: split the head, start the next steps speculatively, or research ahead.")
    elif serial:
        msg = (f"Only one ticket is running ('{tops[head]['title'][:50]}', {head_minutes} min) and nothing else is ready: "
               "is there independent work (research, tests, tooling) that could run beside it?")
    else:
        msg = f"{len(running)} running, {len(ready)} ready, {sum(1 for s in state.values() if s == 'waiting')} waiting on dependencies"
    return {"active": len(active), "running": len(running), "ready": len(ready),
            "waiting": [i for i, s in state.items() if s == "waiting"], "stalled": [i for i, s in state.items() if s == "stalled"],
            "workers_running": workers_running, "capacity": f"{len(running)}/{slots} ticket slots in use",
            "critical_chain": [{"id": i, "title": tops[i]["title"][:60], "state": state[i]} for i in chain],
            "serial": serial, "head_minutes": head_minutes, "message": msg,
            "signature": "|".join(chain) + f"#{len(running)}"}
