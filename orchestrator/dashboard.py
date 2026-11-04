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

from .config import enter_project, load_config

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

        # 2. org tree: managers (top-level assignees) and every worker under their unfinished tickets
        #    (any worker status, so finished ones stay visible while the parent ticket is still open),
        #    in creation order. Workers are numbered per role (surveyor-1, surveyor-2, ...).
        done_states = ("done", "failed", "cancelled")
        by_id = {t["id"]: t for t in tickets}
        org: dict[str, list[dict[str, Any]]] = {}
        for t in tickets:
            if t["parent_id"] is None:
                org.setdefault(t["assignee"], [])
        for t in tickets:
            if t["parent_id"] is None:
                continue
            root = by_id.get(t["path_trace"][1]) if len(t["path_trace"]) > 1 else None
            if t["status"] in ACTIVE or (root is not None and root["status"] not in done_states):
                org.setdefault(root["assignee"] if root else "unknown", []).append(
                    {"worker": t["assignee"], "role": t.get("worker_role"), "ticket": t["id"],
                     "title": t["title"], "status": t["status"]})

        board = [{"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
                  "trace": t["path_trace"], "rigor": t.get("rigor", "balanced"), "parent_id": t["parent_id"]}
                 for t in tickets]

        resources = [dict(r) for r in c.execute("SELECT * FROM resources ORDER BY name")]
        jobs = [{k: r[k] for k in ("id", "name", "state", "resource", "ticket_id", "exit_code", "started_at")}
                for r in c.execute("SELECT * FROM jobs ORDER BY started_at DESC LIMIT 15")]

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
                "resources": resources, "jobs": jobs, "spend": spend, "events": events}
    finally:
        c.close()


def seed_demo(db_path: str | Path) -> None:
    """Fill a fresh database with sample data so every dashboard panel has something to show."""
    from .store import Store
    st = Store(db_path)
    base = dict(worker_role="w", context="c", acceptance_criteria=["a"],
                return_spec={"format": "f", "description": "d"})
    done = st.create_ticket({**base, "title": "Survey serial libraries", "assignee": "research_manager",
                             "rigor": "quick_and_dirty"}, "top_manager")
    st.transition(done["id"], "in_progress", "research_manager")
    w1 = st.create_ticket({**base, "title": "Read pyserial docs", "assignee": "worker:surveyor-1"},
                          "research_manager", parent_id=done["id"])
    st.transition(w1["id"], "in_progress", "worker:surveyor")
    st.transition(w1["id"], "done", "worker:surveyor", result={"summary": "pyserial is the standard"})
    st.add_verification(done["id"], "research_manager", "concerns", ["[deepseek-v4-pro] concerns: no benchmark"])
    st.transition(done["id"], "done", "research_manager", result={"recommendation": "pyserial"})
    tool = st.create_ticket({**base, "title": "Need a serial logger tool", "assignee": "tooling_manager"}, "worker:exp")
    st.transition(tool["id"], "in_progress", "tooling_manager")
    w2 = st.create_ticket({**base, "title": "Find logger on GitHub", "assignee": "worker:tool_scout-1",
                           "worker_role": "tool_scout"}, "tooling_manager", parent_id=tool["id"])
    st.transition(w2["id"], "in_progress", "worker:tool_scout-1")
    w3 = st.create_ticket({**base, "title": "Check PyPI for serial loggers", "assignee": "worker:tool_scout-2",
                           "worker_role": "tool_scout"}, "tooling_manager", parent_id=tool["id"])
    st.transition(w3["id"], "in_progress", "worker:tool_scout-2")
    w4 = st.create_ticket({**base, "title": "Compare two candidates", "assignee": "worker:tool_scout-3",
                           "worker_role": "tool_scout"}, "tooling_manager", parent_id=tool["id"])
    st.transition(w4["id"], "in_progress", "worker:tool_scout-3")
    st.transition(w4["id"], "done", "worker:tool_scout-3", result={"pick": "pyserial-asyncio"})
    risky = st.create_ticket({**base, "title": "Sweep heater power", "assignee": "experiment_manager",
                              "rigor": "rigorous"}, "top_manager")
    st.transition(risky["id"], "in_progress", "experiment_manager")
    st.transition(risky["id"], "escalated", "experiment_manager", note="temperature rising faster than expected")
    q = st.create_ticket({**base, "title": "Pick sampling rate", "assignee": "research_manager"}, "top_manager")
    st.transition(q["id"], "in_progress", "research_manager")
    st.ask_question(q["id"], "research_manager", "What sampling rate does the sensor support?")
    st.register_resource("thermal-rig", "stop between sweep points")
    st.register_resource("scope-1")
    st.claim_resource("thermal-rig", risky["id"], "20m", actor="worker:runner")
    st.create_job(risky["id"], "worker:runner", "heater-sweep", ["python", "sweep.py"], "demo.log",
                  resource="thermal-rig", pid=1)
    st.add_event(None, "deepseek-v4-pro", "usage", {"input_tokens": 120000, "output_tokens": 31000})
    st.close()


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
    ap.add_argument("--project", help="project folder (default: $ORCHESTRATOR_PROJECT or the current directory)")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    ap.add_argument("--demo", action="store_true", help="serve a temporary database filled with sample data")
    a = ap.parse_args()
    enter_project(a.project)
    cfg = load_config(a.config)
    db = a.db or Path(cfg["workspace"]) / "orchestrator.db"
    if a.demo:
        import tempfile
        db = Path(tempfile.mkdtemp(prefix="orch-demo-")) / "demo.db"
        seed_demo(db)
        cfg["budget"]["max_tokens"] = 400000
        print(f"Demo mode: sample data in {db}")
    port = a.port or cfg["interfaces"]["dashboard"]["port"]
    url = f"http://127.0.0.1:{port}"
    print(f"Dashboard serving {db}\n  -> {url}\n  This process keeps running until you press Ctrl+C "
          "(run it in its own terminal window).", flush=True)
    if a.open:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(create_app(db, cfg), host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"Could not start on port {port}: {e}. Try --port with a different number.")
        raise SystemExit(1)
    print("Dashboard stopped.")


if __name__ == "__main__":
    main()
