"""LEARNINGS.md: what was tried and what it taught, so dead ends are not re-walked after restarts or compaction.
Appended automatically from manager results (`dead_ends`, `learned`, refuted/inconclusive experiments) and injected into
the director's and managers' prompts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import append_durable


def path(workspace: str | Path) -> Path:
    return Path(workspace) / "LEARNINGS.md"


def record(workspace: str | Path, ticket_id: str, title: str, result: dict[str, Any] | None, experiment: dict[str, Any] | None = None) -> int:
    """Append dead ends / lessons from a finished ticket. Returns the number of lines added."""
    res = result or {}
    lines: list[str] = []
    for key, tag in (("dead_ends", "DEAD END"), ("learned", "LEARNED")):
        v = res.get(key)
        for x in ([v] if isinstance(v, str) else (v or [])):
            if str(x).strip():
                lines.append(f"- {tag} [{ticket_id}] {title[:60]}: {str(x).strip()[:400]}")
    outcome = str(res.get("outcome") or "")
    if experiment and outcome in ("confirmed", "refuted", "inconclusive"):
        lines.append(f"- EXPERIMENT {outcome.upper()} [{ticket_id}] hypothesis: {str(experiment.get('hypothesis', title))[:300]}"
                     + (f" -> {str(res.get('summary', ''))[:300]}" if res.get("summary") else ""))
    if lines:
        append_durable(path(workspace), "\n".join(lines) + "\n")
    return len(lines)


def read(workspace: str | Path, max_chars: int = 4000) -> str:
    p = path(workspace)
    if not p.is_file():
        return ""
    t = p.read_text(encoding="utf-8")
    return t[-max_chars:]


def section(workspace: str | Path) -> str:
    t = read(workspace)
    return f"# Learnings so far (do not repeat dead ends)\n{t}\n\n" if t.strip() else ""
