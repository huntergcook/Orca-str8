"""Outcome-based model routing: learn from what actually happened.

For every worker ticket that ended (done vs failed/escalated) the model that made its last call gets the credit or the blame,
per department. `reorder` then moves models with a clearly poor record behind the others in a worker's fallback chain.
Deliberately conservative: it needs `routing_min_samples` outcomes for a model, and only demotes (never drops) a model whose
success rate is more than 25 points below the best one in that chain."""
from __future__ import annotations

from typing import Any

from .store import Store


def scorecard(store: Store) -> dict[str, dict[str, dict[str, int]]]:
    """{department: {model: {"done": n, "bad": n}}}"""
    last: dict[str, tuple[str, str]] = {}               # ticket id -> (model, department) of its latest call
    for r in store._conn.execute("SELECT ticket_id, json_extract(payload,'$.model') AS m, json_extract(payload,'$.department') AS d"
                                 " FROM events WHERE type='usage' AND ticket_id IS NOT NULL ORDER BY seq"):
        if r["m"]:
            last[r["ticket_id"]] = (r["m"], r["d"] or "?")
    out: dict[str, dict[str, dict[str, int]]] = {}
    for t in store.list_tickets():
        if t["parent_id"] is None or t["id"] not in last or t["status"] not in ("done", "failed", "escalated"):
            continue
        model, dept = last[t["id"]]
        cell = out.setdefault(dept, {}).setdefault(model, {"done": 0, "bad": 0})
        cell["done" if t["status"] == "done" else "bad"] += 1
    return out


def rate(cell: dict[str, int]) -> float:
    n = cell["done"] + cell["bad"]
    return cell["done"] / n if n else 1.0


def reorder(models: list[str], card: dict[str, dict[str, int]] | None, min_samples: int = 5) -> list[str]:
    if not card or len(models) < 2:
        return models
    known = {m: card[m] for m in models if m in card and card[m]["done"] + card[m]["bad"] >= min_samples}
    if len(known) < 1:
        return models
    best = max(rate(c) for c in known.values())
    poor = [m for m, c in known.items() if best - rate(c) > 0.25]
    if not poor or len(poor) == len(models):
        return models
    return [m for m in models if m not in poor] + [m for m in models if m in poor]
