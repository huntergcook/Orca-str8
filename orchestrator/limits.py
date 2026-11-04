"""Budget enforcement and the pause/kill switch.

GuardedProvider wraps any Provider and refuses to make a call when:
  * a file named PAUSE exists in the workspace (create it to halt all model calls; delete to resume)
  * the configured token or wall-clock budget is exhausted
Budgets default to None (unlimited). max_cost_usd is not enforced yet (needs a price table).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .providers import Completion, Provider
from .store import Store


class Halted(Exception):
    """Raised before a model call when the run is paused or over budget."""


class GuardedProvider:
    def __init__(self, inner: Provider, store: Store, cfg: dict[str, Any], workspace: str | Path) -> None:
        self.inner, self.store, self.cfg = inner, store, cfg
        self.pause_file = Path(workspace) / "PAUSE"

    def tokens_used(self) -> int:
        total = 0
        for e in self.store.events():
            if e["type"] == "usage":
                total += e["payload"].get("input_tokens", 0) + e["payload"].get("output_tokens", 0)
        return total

    def minutes_elapsed(self) -> float:
        evs = self.store.events()
        if not evs:
            return 0.0
        first = datetime.fromisoformat(evs[0]["ts"])
        return (datetime.now(timezone.utc) - first).total_seconds() / 60

    def check(self) -> None:
        if self.pause_file.exists():
            raise Halted(f"paused: remove {self.pause_file} to resume")
        b = self.cfg["budget"]
        if b.get("max_tokens") is not None and self.tokens_used() >= b["max_tokens"]:
            raise Halted(f"token budget exhausted ({b['max_tokens']})")
        if b.get("max_minutes") is not None and self.minutes_elapsed() >= b["max_minutes"]:
            raise Halted(f"time budget exhausted ({b['max_minutes']} min)")

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        self.check()
        return self.inner.complete(model=model, system=system, messages=messages, max_tokens=max_tokens)
