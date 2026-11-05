"""The homecoming report: one page for the human who walked away.

Mission goal and success criteria, how the project stands, every decision the director made on the human's behalf, what was
learned and what dead-ended, the work done per department, cost and time, and what is still open. Written to
workspace/REPORT.md (regenerated any time with `ctl report`; the daemon refreshes it when something finishes)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import critical_path, learnings, mission
from .limits import cost_estimate
from .store import Store
from .util import atomic_write


def _tail(p: Path, n: int = 4000) -> str:
    return p.read_text(encoding="utf-8")[-n:].strip() if p.is_file() else ""


def build(store: Store, cfg: dict[str, Any], workspace: str | Path) -> str:
    ws = Path(workspace)
    tops = [t for t in store.list_tickets() if t["parent_id"] is None]
    by: dict[str, int] = {}
    for t in tops:
        by[t["status"]] = by.get(t["status"], 0) + 1
    out = ["# Project report", ""]
    finished = (ws / "FINISHED").is_file()
    out.append(f"**Status:** {'FINISHED - ' + _tail(ws / 'FINISHED', 600) if finished else 'in progress'}")
    out.append(f"**Tickets:** " + (", ".join(f"{v} {k}" for k, v in sorted(by.items())) or "none yet"))
    goal, crit = mission.answer_of(cfg, "goal"), mission.answer_of(cfg, "success_criteria")
    if goal:
        out += ["", "## Goal", str(goal).strip()]
    if crit:
        out += ["", "## Success criteria"] + [f"- {c}" for c in (crit if isinstance(crit, list) else str(crit).splitlines()) if str(c).strip()]
    ev = sorted((ws / "evidence").glob("*.txt")) if (ws / "evidence").is_dir() else []
    if ev:
        out += ["", "## Latest evidence (success checks)", "```", ev[-1].read_text(encoding="utf-8")[-2500:].strip(), "```"]
    dec = _tail(ws / "DECISIONS.md")
    if dec:
        out += ["", "## Decisions the director made on your behalf", dec]
    les = learnings.read(ws, 3000).strip()
    if les:
        out += ["", "## Learned / dead ends", les]
    done = [t for t in tops if t["status"] == "done"]
    if done:
        out += ["", "## Completed work"] + [f"- {t['title'][:90]} ({t['assignee']})" for t in done[-30:]]
    open_ = [t for t in tops if t["status"] not in ("done", "cancelled")]
    if open_:
        out += ["", "## Still open"] + [f"- [{t['status']}] {t['title'][:90]}" for t in open_]
    ana = critical_path.analyze(store)
    out += ["", "## Pace", ana["message"]]
    try:
        cost = cost_estimate(store, cfg)
    except Exception:
        cost = None
    tot = store.usage_totals()
    out += ["", "## Usage", f"{tot['calls']} model calls, {tot['input_tokens']:,} tokens in, {tot['output_tokens']:,} out"
            + (f", estimated ${cost:.2f}" if cost is not None else "")]
    drift = _tail(ws / "drift.json", 800)
    if drift and '"on_track": false' in drift:
        out += ["", "## Open drift warning", drift]
    return "\n".join(out) + "\n"


def write(store: Store, cfg: dict[str, Any], workspace: str | Path) -> Path:
    p = Path(workspace) / "REPORT.md"
    atomic_write(p, build(store, cfg, workspace))
    return p
