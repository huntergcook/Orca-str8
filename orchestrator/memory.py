"""File-based agent memory (markdown in the workspace). Managers write their own;
the director can edit anyone's. Layout:

    <workspace>/memory/<agent>/state.md     current state, rewritten as needed
    <workspace>/memory/<agent>/journal.md   append-only timestamped journal
    <workspace>/memory/human_log.md         director's log of human interaction
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .util import append_durable, atomic_write


class Memory:
    def __init__(self, workspace: str | Path, agent: str) -> None:
        self.dir = Path(workspace) / "memory" / agent
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.md"
        self.journal_path = self.dir / "journal.md"

    def read_state(self) -> str:
        return self.state_path.read_text(encoding="utf-8") if self.state_path.exists() else ""

    def write_state(self, text: str) -> None:
        atomic_write(self.state_path, text)

    def append_journal(self, text: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        append_durable(self.journal_path, f"\n## {ts}\n{text.strip()}\n")

    def read_journal(self, last_chars: int = 6000) -> str:
        if not self.journal_path.exists():
            return ""
        return self.journal_path.read_text(encoding="utf-8")[-last_chars:]

    def resume_brief(self) -> str:
        """What a restarted manager reads first."""
        return f"# Current state\n{self.read_state() or '(none yet)'}\n\n# Recent journal\n{self.read_journal() or '(empty)'}"


def human_log(workspace: str | Path) -> Path:
    p = Path(workspace) / "memory" / "human_log.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
