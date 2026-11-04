"""Budgets, the pause switch, and a cap on concurrent model calls.

GuardedProvider wraps any Provider. Before each call it refuses (raises Halted) when:
  * a file named PAUSE exists in the workspace (create it to halt all model calls; delete to resume)
  * the token or dollar budget (cumulative for the workspace) or the per-run time budget is exhausted
    (dollars need a `prices` table in the config)
It also lets at most `policies.max_concurrent_calls` calls run at once, so parallel managers and workers do not
trip the provider's rate limits. Budgets default to None (unlimited).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .providers import Completion, Provider
from .store import Store


class Halted(Exception):
    """Raised before a model call when the run is paused or over budget."""


def estimate_cost(by_model: dict[str, dict[str, int]], prices: dict[str, Any]) -> float | None:
    """USD from per-model token counts and a price table {model: {input, output, cache_hit}} in $ per million tokens.
    None if no model in use has a price."""
    total, priced = 0.0, False
    for model, u in by_model.items():
        p = prices.get(model) or prices.get(model.split(":", 1)[-1])
        if not p:
            continue
        priced = True
        hit = u.get("cache_hit_tokens", 0)
        miss = max(0, u.get("input_tokens", 0) - hit)
        total += (miss * float(p.get("input", 0)) + hit * float(p.get("cache_hit", p.get("input", 0)))
                  + u.get("output_tokens", 0) * float(p.get("output", 0))) / 1_000_000
    return total if priced else None


class GuardedProvider:
    def __init__(self, inner: Provider, store: Store, cfg: dict[str, Any], workspace: str | Path,
                 max_concurrent: int | None = None) -> None:
        self.inner, self.store, self.cfg = inner, store, cfg
        self.pause_file = Path(workspace) / "PAUSE"
        n = max_concurrent or (cfg.get("policies") or {}).get("max_concurrent_calls") or 8
        self._slots = threading.BoundedSemaphore(max(1, int(n)))
        self._started = time.monotonic()          # max_minutes limits THIS run, not idle time between runs

    def tokens_used(self) -> int:
        t = self.store.usage_totals()
        return t["input_tokens"] + t["output_tokens"]

    def cost_used(self) -> float | None:
        return estimate_cost(self.store.usage_by_model(), self.cfg.get("prices") or {})

    def minutes_elapsed(self) -> float:
        """Minutes since this run started (a new run gets a fresh time budget; tokens and dollars are cumulative)."""
        return (time.monotonic() - self._started) / 60

    def check(self) -> None:
        if self.pause_file.exists():
            raise Halted(f"paused: remove {self.pause_file} to resume")
        b = self.cfg["budget"]
        if b.get("max_tokens") is not None and self.tokens_used() >= b["max_tokens"]:
            raise Halted(f"token budget exhausted ({b['max_tokens']})")
        if b.get("max_minutes") is not None and self.minutes_elapsed() >= b["max_minutes"]:
            raise Halted(f"time budget exhausted ({b['max_minutes']} min)")
        if b.get("max_cost_usd") is not None:
            cost = self.cost_used()
            if cost is not None and cost >= float(b["max_cost_usd"]):
                raise Halted(f"cost budget exhausted (${cost:.2f} of ${float(b['max_cost_usd']):.2f})")

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        self.check()
        with self._slots:                 # waits here when max_concurrent_calls are already in flight
            self.check()                  # the budget may have run out while waiting
            return self.inner.complete(model=model, system=system, messages=messages, max_tokens=max_tokens)
