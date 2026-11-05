"""Budgets, the pause switch, and a cap on concurrent model calls.

GuardedProvider wraps any Provider. Before each call it refuses (raises Halted) when:
  * a file named PAUSE exists in the workspace (create it to halt all model calls; delete to resume)
  * the token or dollar budget (cumulative for the workspace) or the per-run time budget is exhausted
    (dollars need a `prices` table in the config; off-peak rates with `price_off_peak_utc`)
  * a provider's REAL account balance (DeepSeek reports one) is below `budget.stop_below_balance_usd`
It also lets at most `policies.max_concurrent_calls` calls run at once, so parallel managers and workers do not
trip the provider's rate limits. Budgets default to None (unlimited).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .providers import Completion, ModelUnavailable, Provider, fetch_balances
from .store import Store


class Halted(Exception):
    """Raised before a model call when the run is paused or over budget."""


def _price(prices: dict[str, Any], model: str) -> dict[str, Any] | None:
    return prices.get(model) or prices.get(model.split(":", 1)[-1])


def _rates(p: dict[str, Any], off_peak: bool) -> dict[str, float]:
    """$ per million tokens. An `off_peak` entry is either a multiplier (0.5 = half price) or its own rate table."""
    base = {k: float(p.get(k, p.get("input", 0) if k == "cache_hit" else 0)) for k in ("input", "cache_hit", "output")}
    base["cache_write"] = float(p.get("cache_write", base["input"] * 1.25))   # Anthropic's 5-minute cache write
    if not off_peak or "off_peak" not in p:
        return base
    op = p["off_peak"]
    if isinstance(op, (int, float)):
        return {k: v * float(op) for k, v in base.items()}
    return {k: float(op.get(k, base[k])) for k in base}


def _cost(u: dict[str, int], r: dict[str, float]) -> float:
    """input_tokens counts the whole prompt: cache reads + cache writes + the uncached rest."""
    hit, wr = u.get("cache_hit_tokens", 0) or 0, u.get("cache_write_tokens", 0) or 0
    miss = max(0, u.get("input_tokens", 0) - hit - wr)
    return (miss * r["input"] + hit * r["cache_hit"] + wr * r.get("cache_write", r["input"] * 1.25)
            + u.get("output_tokens", 0) * r["output"]) / 1_000_000


def estimate_cost(by_model: dict[str, dict[str, int]], prices: dict[str, Any]) -> float | None:
    """USD from per-model token counts and a price table {model: {input, output, cache_hit}} in $ per million tokens
    (all at the normal rate). None if no model in use has a price."""
    total, priced = 0.0, False
    for model, u in by_model.items():
        p = _price(prices, model)
        if p:
            priced, total = True, total + _cost(u, _rates(p, False))
    return total if priced else None


def estimate_cost_split(split: dict[str, dict[str, dict[str, int]]], prices: dict[str, Any]) -> float | None:
    """Like estimate_cost, for usage split into peak and off-peak calls (Store.usage_by_model_split)."""
    total, priced = 0.0, False
    for model, periods in split.items():
        p = _price(prices, model)
        if not p:
            continue
        priced = True
        for period, u in periods.items():
            total += _cost(u, _rates(p, period == "off_peak"))
    return total if priced else None


def cost_by_model(store: Store, cfg: dict[str, Any]) -> dict[str, float]:
    """Estimated USD per model (off-peak aware), for models with a price."""
    prices = cfg.get("prices") or {}
    window = cfg.get("price_off_peak_utc")
    out: dict[str, float] = {}
    if window and len(window) == 2:
        for model, periods in store.usage_by_model_split((str(window[0]), str(window[1]))).items():
            p = _price(prices, model)
            if p:
                out[model] = sum(_cost(u, _rates(p, period == "off_peak")) for period, u in periods.items())
    else:
        for model, u in store.usage_by_model().items():
            p = _price(prices, model)
            if p:
                out[model] = _cost(u, _rates(p, False))
    return out


def cost_estimate(store: Store, cfg: dict[str, Any]) -> float | None:
    """The workspace's estimated spend: off-peak calls are priced at the off-peak rate when the config has
    `price_off_peak_utc: ["HH:MM", "HH:MM"]` and the model's price has an `off_peak` entry."""
    prices = cfg.get("prices") or {}
    window = cfg.get("price_off_peak_utc")
    if window and len(window) == 2:
        return estimate_cost_split(store.usage_by_model_split((str(window[0]), str(window[1]))), prices)
    return estimate_cost(store.usage_by_model(), prices)


class GuardedProvider:
    def __init__(self, inner: Provider, store: Store, cfg: dict[str, Any], workspace: str | Path,
                 max_concurrent: int | None = None, balance_fn: Any | None = None) -> None:
        self.inner, self.store, self.cfg = inner, store, cfg
        self.pause_file = Path(workspace) / "PAUSE"
        n = max_concurrent or (cfg.get("policies") or {}).get("max_concurrent_calls") or 8
        self._slots = threading.BoundedSemaphore(max(1, int(n)))
        self._started = time.monotonic()          # max_minutes limits THIS run, not idle time between runs
        self._balance_fn = balance_fn or (lambda: fetch_balances(cfg))
        self._balance_at, self._balances = -1e9, {}
        self._balance_lock = threading.Lock()

    def tokens_used(self) -> int:
        t = self.store.usage_totals()
        return t["input_tokens"] + t["output_tokens"]

    def cost_used(self) -> float | None:
        return cost_estimate(self.store, self.cfg)

    def minutes_elapsed(self) -> float:
        """Minutes since this run started (a new run gets a fresh time budget; tokens and dollars are cumulative)."""
        return (time.monotonic() - self._started) / 60

    def balances(self) -> dict[str, dict[str, Any]]:
        """Real account balances, re-read at most every `budget.balance_check_seconds` (default 60)."""
        every = float(self.cfg["budget"].get("balance_check_seconds", 60))
        with self._balance_lock:
            if time.monotonic() - self._balance_at >= every:
                try:
                    self._balances = self._balance_fn() or {}
                except Exception:
                    self._balances = {}
                self._balance_at = time.monotonic()
            return dict(self._balances)

    def check(self) -> None:
        if self.pause_file.exists():
            raise Halted(f"paused: remove {self.pause_file} to resume")
        b = self.cfg["budget"]
        if b.get("max_tokens") is not None and self.tokens_used() >= b["max_tokens"]:
            raise Halted(f"token budget exhausted ({b['max_tokens']})")
        if b.get("max_minutes") is not None and self.minutes_elapsed() >= b["max_minutes"]:
            raise Halted(f"time budget exhausted ({b['max_minutes']} min)")
        floor = b.get("stop_below_balance_usd")
        if floor is not None:
            for name, info in self.balances().items():
                if "usd" in info and info["usd"] < float(floor):
                    raise Halted(f"{name} balance ${info['usd']:.2f} is below the floor ${float(floor):.2f} "
                                 "(budget.stop_below_balance_usd)")
        if b.get("max_cost_usd") is not None:
            cost = self.cost_used()
            if cost is not None and cost >= float(b["max_cost_usd"]):
                raise Halted(f"cost budget exhausted (${cost:.2f} of ${float(b['max_cost_usd']):.2f})")

    def model_cap_exhausted(self, model: str) -> str | None:
        """budget.max_cost_usd_by_model: {model id or prefix: USD}. A model past its cap is skipped (ModelUnavailable),
        so its tickets fall through to the next model in the chain instead of halting the whole run."""
        caps = self.cfg["budget"].get("max_cost_usd_by_model") or {}
        bare = model.split(":", 1)[-1]
        mine = {k: v for k, v in caps.items() if model.startswith(str(k)) or bare.startswith(str(k))}
        if not mine:
            return None
        spent = cost_by_model(self.store, self.cfg)
        for prefix, cap in mine.items():
            used = sum(v for m, v in spent.items()
                       if m.startswith(str(prefix)) or m.split(":", 1)[-1].startswith(str(prefix)))
            if used >= float(cap):
                return f"cost cap for {prefix}* reached (${used:.2f} of ${float(cap):.2f}; budget.max_cost_usd_by_model)"
        return None

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        self.check()
        why = self.model_cap_exhausted(model)
        if why:
            raise ModelUnavailable(why)
        with self._slots:                 # waits here when max_concurrent_calls are already in flight
            self.check()                  # the budget may have run out while waiting
            return self.inner.complete(model=model, system=system, messages=messages, max_tokens=max_tokens)
