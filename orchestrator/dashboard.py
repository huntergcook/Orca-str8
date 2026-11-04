"""Read-only dashboard: FastAPI + one static page. Reads the SQLite store via a
read-only connection; it can never modify orchestrator state.

    python -m orchestrator.dashboard [--db PATH] [--config PATH] [--port 8080]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import load_config

STATIC = Path(__file__).parent / "static" / "index.html"
ACTIVE = ("in_progress", "needs_clarification", "blocked", "escalated")


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def build_state(db_path: str | Path, cfg: dict[str, Any], event_limit: int = 40) -> dict[str, Any]:
    c = _connect_ro(db_path)
    try:
        tickets = [json.loads(r["data"]) for r in c.execute("SELECT data FROM tickets ORDER BY created_at")]
        by_status: dict[str, int] = {}
        for t in tickets:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1

        # 1. needs human: escalations, plus questions asked upward from top-level tickets
        needs_human = [
            {"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
             "detail": next((q["question"] for q in t["questions"] if "answer" not in q), "")}
            for t in tickets
            if t["status"] == "escalated" or (t["status"] == "needs_clarification" and t["parent_id"] is None)
        ]

        # 2. org tree: managers (top-level assignees) and their live workers
        org: dict[str, list[dict[str, Any]]] = {}
        for t in tickets:
            if t["parent_id"] is None:
                org.setdefault(t["assignee"], [])
        for t in tickets:
            if t["parent_id"] is not None and t["status"] in ACTIVE:
                root = t["path_trace"][1] if len(t["path_trace"]) > 1 else None
                owner = next((p["assignee"] for p in tickets if p["id"] == root), None) if root else None
                org.setdefault(owner or "unknown", []).append(
                    {"worker": t["assignee"], "ticket": t["id"], "title": t["title"], "status": t["status"]})

        board = [{"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
                  "trace": t["path_trace"], "rigor": t.get("rigor", "balanced"), "parent_id": t["parent_id"]}
                 for t in tickets]

        resources = [dict(r) for r in c.execute("SELECT * FROM resources ORDER BY name")]

        tokens_in = tokens_out = 0
        for r in c.execute("SELECT payload FROM events WHERE type='usage'"):
            p = json.loads(r["payload"])
            tokens_in += p.get("input_tokens", 0)
            tokens_out += p.get("output_tokens", 0)
        spend = {"input_tokens": tokens_in, "output_tokens": tokens_out,
                 "max_tokens": cfg["budget"].get("max_tokens"), "max_cost_usd": cfg["budget"].get("max_cost_usd")}

        events = [{**dict(r), "payload": json.loads(r["payload"])} for r in c.execute(
            "SELECT * FROM events WHERE type != 'usage' ORDER BY seq DESC LIMIT ?", (event_limit,))]

        return {"needs_human": needs_human, "org": org, "by_status": by_status, "board": board,
                "resources": resources, "spend": spend, "events": events}
    finally:
        c.close()


def create_app(db_path: str | Path, cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = cfg or load_config(None)
    app = FastAPI(title="Orchestrator dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return build_state(db_path, cfg)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC.read_text(encoding="utf-8")

    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(prog="orchestrator.dashboard")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    db = a.db or Path(cfg["workspace"]) / "orchestrator.db"
    port = a.port or cfg["interfaces"]["dashboard"]["port"]
    uvicorn.run(create_app(db, cfg), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
