"""Dashboard: FastAPI + one static page. State is read through a read-only SQLite connection.
The only way it changes anything is POST /api/action (answer / retry / cancel a ticket that needs
attention), which is guarded by a per-start secret token embedded in the page it serves plus a
localhost Host check, and the server binds to 127.0.0.1 only.

    python -m orchestrator.dashboard [--db PATH] [--config PATH] [--port 8080]
"""
from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import actions
from .config import enter_project, load_config
from .store import Store, StoreError

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
        def why(ticket_id: str) -> str:
            evs = [{"type": r["type"], "payload": json.loads(r["payload"])} for r in
                   c.execute("SELECT type, payload FROM events WHERE ticket_id=? ORDER BY seq", (ticket_id,))]
            return actions.why_from_events(evs)

        def item(t: dict[str, Any]) -> dict[str, Any]:
            return {"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
                    "level": "top" if t["parent_id"] is None else "worker", "route": actions.route_of(t),
                    "detail": next((q["question"] for q in t["questions"] if "answer" not in q), ""),
                    "why": why(t["id"])}

        # Only what reached the human: safety/refusals by policy, or handed off by the top manager.
        # Everything else is still being handled by a manager or the top manager.
        attention = [item(t) for t in tickets if actions.route_of(t)]
        needs_human = [i for i in attention if i["route"] == "human"]
        handling = [i for i in attention if i["route"] != "human"]

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

        last_code: dict[str, str] = {}
        for r in c.execute("SELECT ticket_id, type FROM events WHERE type IN ('code_ready','code_accepted','code_rejected','code_discarded') ORDER BY seq"):
            last_code[r["ticket_id"]] = r["type"]
        code_pending = []
        for t in tickets:
            if last_code.get(t["id"]) == "code_ready":
                files = ((t.get("result") or {}).get("code") or {}).get("files", [])
                code_pending.append({"id": t["id"], "title": t["title"], "files": files,
                                     "summary": str((t.get("result") or {}).get("summary", ""))[:200]})

        board = [{"id": t["id"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
                  "trace": t["path_trace"], "rigor": t.get("rigor", "balanced"), "parent_id": t["parent_id"]}
                 for t in tickets]

        resources = [dict(r) for r in c.execute("SELECT * FROM resources ORDER BY name")]
        try:
            agents = [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY department, kind DESC, name")]
        except sqlite3.OperationalError:          # a database from before the agents table
            agents = []
        titles = {t["id"]: t["title"] for t in tickets}
        for a in agents:
            a["ticket_title"] = titles.get(a["ticket_id"] or "", "")
        by_id = {t["id"]: t for t in tickets}

        def dept_of(actor: str, ticket_id: str | None) -> str:
            if actor.endswith("_manager"):
                return actor.removesuffix("_manager")
            if actor == "verifier":
                return "verification"
            t = by_id.get(ticket_id or "")
            while t and t.get("parent_id"):
                t = by_id.get(t["parent_id"])
            a = (t or {}).get("assignee", "")
            return a.removesuffix("_manager") if a.endswith("_manager") else "unattributed"

        usage_by_dept: dict[str, dict[str, int]] = {}
        for r in c.execute("SELECT actor, ticket_id, payload FROM events WHERE type='usage'"):
            p = json.loads(r["payload"])
            d = p.get("department") or dept_of(r["actor"], r["ticket_id"])
            u = usage_by_dept.setdefault(d, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0})
            u["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_hit_tokens"):
                u[k] += int(p.get(k, 0) or 0)
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

        integration = None
        try:
            from .codework import code_repo_from_cfg
            repo = code_repo_from_cfg(cfg)
            integration = repo.integration_status() if repo else None
        except Exception:
            integration = None
        return {"needs_human": needs_human, "handling": handling, "code_pending": code_pending, "org": org, "by_status": by_status, "board": board,
                "resources": resources, "jobs": jobs, "spend": spend, "events": events, "agents": agents,
                "usage_by_department": usage_by_dept, "integration": integration}
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
    st.escalate(risky["id"], "experiment_manager", "temperature rising faster than expected", kind="safety")
    q = st.create_ticket({**base, "title": "Pick sampling rate", "assignee": "research_manager"}, "top_manager")
    st.transition(q["id"], "in_progress", "research_manager")
    st.ask_question(q["id"], "research_manager", "What sampling rate does the sensor support?")
    st.handoff(q["id"], "top_manager", "I do not know the sensor's rate; only you do")
    stuck = st.create_ticket({**base, "title": "Read mpremote docs", "assignee": "worker:tool_scout-4",
                              "worker_role": "tool_scout"}, "tooling_manager", parent_id=tool["id"])
    st.transition(stuck["id"], "in_progress", "worker:tool_scout-4")
    st.escalate(stuck["id"], "worker:tool_scout-4", "could not obtain a valid reply within retry limit",
                kind="technical")
    st.register_resource("thermal-rig", "stop between sweep points")
    st.register_resource("scope-1")
    st.claim_resource("thermal-rig", risky["id"], "20m", actor="worker:runner")
    st.create_job(risky["id"], "worker:runner", "heater-sweep", ["python", "sweep.py"], "demo.log",
                  resource="thermal-rig", pid=1)
    st.add_event(None, "deepseek-v4-pro", "usage", {"input_tokens": 120000, "output_tokens": 31000})
    st.close()


class ActionIn(BaseModel):
    action: str
    ticket_id: str
    text: str = ""


def create_app(db_path: str | Path, cfg: dict[str, Any] | None = None, launch_run: Any = None,
               project: str | Path | None = None, token: str | None = None) -> FastAPI:
    cfg = cfg or load_config(None)
    token = token or secrets.token_urlsafe(24)
    project = Path(project) if project else Path.cwd()
    workspace = Path(db_path).resolve().parent
    launch = launch_run or (lambda: actions.spawn_run(project, workspace))
    app = FastAPI(title="Orchestrator dashboard", docs_url=None, redoc_url=None)
    app.state.token = token

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        out = build_state(db_path, cfg)
        out["run_active"] = actions.run_is_active(workspace)
        # work that needs a run but has none: typical after a crash, restart or power loss
        out["unfinished"] = [] if out["run_active"] else [
            {"id": t["id"], "title": t["title"], "status": t["status"]} for t in out["board"]
            if t["status"] in ("open", "in_progress") and t["parent_id"] is None]
        return out

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC.read_text(encoding="utf-8").replace("__DASH_TOKEN__", token)

    def guard(request: Request) -> JSONResponse | None:
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "bad host"}, status_code=403)
        if not secrets.compare_digest(request.headers.get("x-dashboard-token", ""), token):
            return JSONResponse({"error": "bad or missing token; reload the page"}, status_code=403)
        return None

    @app.post("/api/resume")
    def resume(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        if actions.run_is_active(workspace):
            return JSONResponse({"error": "a run is already active"}, status_code=409)
        try:
            launch()
        except OSError as e:
            return JSONResponse({"error": f"could not start a run: {e}"}, status_code=500)
        return JSONResponse({"run_started": True})

    @app.post("/api/action")
    def act(body: ActionIn, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        fn = actions.ACTIONS.get(body.action)
        if fn is None:
            return JSONResponse({"error": f"unknown action {body.action!r}"}, status_code=400)
        store = Store(db_path)
        try:
            result = fn(store, body.ticket_id, body.text, "human")
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            store.close()
        started = False
        if body.action in ("answer", "retry") and not actions.run_is_active(workspace):
            try:
                launch()                      # pick the work back up without waiting for anyone
                started = True
            except OSError as e:
                result["warning"] = f"could not start a run: {e}"
        return JSONResponse({**result, "run_started": started})

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
        uvicorn.run(create_app(db, cfg, project=Path.cwd()), host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"Could not start on port {port}: {e}. Try --port with a different number.")
        raise SystemExit(1)
    print("Dashboard stopped.")


if __name__ == "__main__":
    main()
