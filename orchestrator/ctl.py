"""Control interface for an EXTERNAL top manager (e.g. an interactive Claude Code session).

The external agent plays the top manager and talks to the human; this tool is its hands:
it creates tickets, runs the department managers (workers/verifiers run on the configured
API providers), reads results, answers questions and keeps durable notes. All state is in
files and SQLite, so a fresh session can resume with `resume`.

    python -m orchestrator.ctl [--config PATH] <command> [args]      (JSON on stdout)

Commands: resume | board | ticket ID | create [--file F | stdin JSON] | run | answer ID TEXT |
          retry ID TEXT | state [--set (stdin)] | log TEXT | finish SUMMARY | pause | unpause
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .hardware import hardware_brief
from .limits import GuardedProvider, Halted
from .memory import human_log
from .providers import Provider, build_provider
from .store import Store, StoreError
from .top import TopManager


class Ctl:
    def __init__(self, cfg: dict[str, Any], workspace: str | Path, provider: Provider | None = None) -> None:
        self.cfg, self.workspace = cfg, Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.workspace / "orchestrator.db")
        self.provider = GuardedProvider(provider or build_provider(cfg), self.store, cfg, self.workspace)
        self.top = TopManager(self.store, self.provider, cfg, self.workspace)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ reading

    def _row(self, t: dict[str, Any], full: bool = False) -> dict[str, Any]:
        kids = self.store.list_tickets(parent_id=t["id"])
        row = {"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
               "rigor": t.get("rigor", "balanced"), "children": {k["status"]: 0 for k in kids}}
        for k in kids:
            row["children"][k["status"]] += 1
        if t["result"] is not None:
            row["result"] = t["result"]
        q = [x["question"] for x in t["questions"] if "answer" not in x]
        if q:
            row["open_question"] = q[0]
        if t.get("verification_notes"):
            row["verification_notes"] = t["verification_notes"]
        return row

    def board(self) -> dict[str, Any]:
        top_level = [t for t in self.store.list_tickets() if t["parent_id"] is None]
        rows = [self._row(t) for t in top_level]
        attention = []
        for t in self.store.list_tickets():
            if t["status"] in ("escalated", "needs_clarification"):
                ev = [e for e in self.store.events(t["id"]) if e["type"] in ("status_changed", "refusal_escalated")]
                attention.append({**self._row(t), "level": "top" if t["parent_id"] is None else "worker",
                                  "why": (ev[-1]["payload"].get("note") or ev[-1]["payload"]) if ev else ""})
        return {"tickets": rows, "needs_attention": attention, "halted": self._halt_reason()}

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        t = self.store.get_ticket(ticket_id)
        return {"ticket": t, "children": [self._row(k) for k in self.store.list_tickets(parent_id=ticket_id)],
                "events": [e for e in self.store.events(ticket_id) if e["type"] != "usage"][-25:]}

    def _halt_reason(self) -> str | None:
        try:
            self.provider.check()
            return None
        except Halted as h:
            return str(h)

    def resume(self) -> dict[str, Any]:
        """Everything a fresh top manager needs to pick up the work."""
        log = human_log(self.workspace)
        return {"departments": list(self.top.managers),
                "state_notes": self.top.memory.read_state() or "(none yet)",
                "journal_tail": self.top.memory.read_journal(3000),
                "human_log_tail": log.read_text(encoding="utf-8")[-3000:] if log.exists() else "",
                "hardware": hardware_brief(self.store) or "(none)",
                "finished": (self.workspace / "FINISHED").exists(),
                **self.board()}

    # ------------------------------------------------------------------ acting

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        spec = {"worker_role": "worker", **spec}
        tid = self.top.create_ticket(spec)
        if tid is None:
            raise StoreError(f"unknown department {spec.get('department')!r}; have {list(self.top.managers)}")
        return {"ticket_id": tid}

    def run(self) -> dict[str, Any]:
        """Run department managers on all open/in-progress tickets. May take a long time."""
        before = {t["id"]: t["status"] for t in self.store.list_tickets()}
        halted = None
        try:
            self.top.pump()
        except Halted as h:
            halted = str(h)
        after = self.board()
        changed = [{"id": t["id"], "title": t["title"], "from": before.get(t["id"]), "to": t["status"]}
                   for t in self.store.list_tickets()
                   if t["parent_id"] is None and before.get(t["id"]) != t["status"]]
        return {"halted": halted, "changed": changed, **after}

    def answer(self, ticket_id: str, text: str) -> dict[str, Any]:
        t = self.store.answer_question(ticket_id, "top_manager", text)
        return {"ticket_id": ticket_id, "status": t["status"]}

    def retry(self, ticket_id: str, text: str) -> dict[str, Any]:
        """Send an escalated ticket back to work with guidance appended to its context."""
        self.store.append_context(ticket_id, "top_manager", text)
        t = self.store.transition(ticket_id, "in_progress", "top_manager", note="retry with guidance")
        return {"ticket_id": ticket_id, "status": t["status"]}

    def state(self, set_text: str | None = None) -> dict[str, Any]:
        if set_text is not None:
            self.top.memory.write_state(set_text)
        return {"state": self.top.memory.read_state()}

    def log(self, text: str) -> dict[str, Any]:
        self.top.memory.append_journal(text)
        return {"logged": True}

    def finish(self, summary: str) -> dict[str, Any]:
        self.top.memory.append_journal(f"FINISHED: {summary}")
        (self.workspace / "FINISHED").write_text(summary, encoding="utf-8")
        return {"finished": True}

    def pause(self, on: bool) -> dict[str, Any]:
        p = self.workspace / "PAUSE"
        if on:
            p.write_text("paused by top manager", encoding="utf-8")
        elif p.exists():
            p.unlink()
        return {"paused": on}


def main(argv: list[str] | None = None, provider: Provider | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.ctl")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("resume", "board", "run", "pause", "unpause"):
        sub.add_parser(name)
    sub.add_parser("ticket").add_argument("id")
    c = sub.add_parser("create")
    c.add_argument("--file", help="JSON file (default: read JSON from stdin)")
    for name in ("answer", "retry"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("text")
    s = sub.add_parser("state")
    s.add_argument("--set", action="store_true", help="replace state notes with stdin")
    sub.add_parser("log").add_argument("text")
    sub.add_parser("finish").add_argument("summary")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    ctl = Ctl(cfg, cfg["workspace"], provider)
    try:
        if a.cmd == "create":
            raw = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
            out = ctl.create(json.loads(raw))
        elif a.cmd == "state":
            out = ctl.state(sys.stdin.read() if a.set else None)
        elif a.cmd in ("answer", "retry"):
            out = getattr(ctl, a.cmd)(a.id, a.text)
        elif a.cmd == "ticket":
            out = ctl.ticket(a.id)
        elif a.cmd == "log":
            out = ctl.log(a.text)
        elif a.cmd == "finish":
            out = ctl.finish(a.summary)
        elif a.cmd in ("pause", "unpause"):
            out = ctl.pause(a.cmd == "pause")
        else:
            out = getattr(ctl, a.cmd)()
        print(json.dumps(out, indent=2, default=str))
        return 0
    except (StoreError, json.JSONDecodeError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    finally:
        ctl.close()


if __name__ == "__main__":
    sys.exit(main())
