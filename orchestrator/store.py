"""SQLite-backed ticket, event and resource store.

Design (see DESIGN.md):
  * tickets   - current state of every ticket (JSON document + indexed columns)
  * events    - append-only audit log; UPDATE/DELETE are blocked by triggers
  * resources - physical hardware flags owned by the Experiment manager
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "ticket.schema.json"

TERMINAL = {"done", "failed", "cancelled"}

# Escalation chain: worker -> department manager -> top manager -> human.
# Safety issues and model refusals are the human's decision by policy, so they skip the chain.
HUMAN_ONLY_KINDS = {"safety", "refusal"}


def route_for(ticket: dict, kind: str = "blocked") -> str:
    """Who must handle a newly escalated ticket: 'manager', 'top_manager' or 'human'."""
    if kind in HUMAN_ONLY_KINDS:
        return "human"
    return "manager" if ticket.get("parent_id") else "top_manager"

TRANSITIONS: dict[str, set[str]] = {
    "open": {"in_progress", "blocked", "escalated", "cancelled"},
    "in_progress": {"needs_clarification", "blocked", "escalated", "done", "failed", "cancelled"},
    "needs_clarification": {"in_progress", "escalated", "cancelled"},
    "blocked": {"in_progress", "escalated", "cancelled"},
    "escalated": {"in_progress", "failed", "cancelled"},
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}

DDL = """
CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    assignee   TEXT NOT NULL,
    created_by TEXT NOT NULL,
    parent_id  TEXT,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_status   ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets(assignee);
CREATE INDEX IF NOT EXISTS idx_tickets_parent   ON tickets(parent_id);

CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TABLE IF NOT EXISTS jobs (
    id        TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    actor     TEXT NOT NULL,
    name      TEXT NOT NULL,
    command   TEXT NOT NULL,
    resource  TEXT,
    pid       INTEGER,
    pid_started REAL,
    state     TEXT NOT NULL DEFAULT 'running',  -- running | exited | stopped | lost
    exit_code INTEGER,
    log_path  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_ticket ON jobs(ticket_id);

CREATE TABLE IF NOT EXISTS resources (
    name              TEXT PRIMARY KEY,
    state             TEXT NOT NULL DEFAULT 'free',  -- free | reserved | busy
    holder_ticket     TEXT,
    expected_duration TEXT,
    started_at        TEXT,
    last_heartbeat    TEXT,
    safe_to_stop      TEXT NOT NULL DEFAULT ''
);
"""


class StoreError(Exception):
    """Base class for store errors."""


class NotFound(StoreError):
    pass


class InvalidTransition(StoreError):
    pass


class ResourceBusy(StoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str | Path = "orchestrator.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")      # a committed change survives a power cut
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(DDL)
        if "pid_started" not in {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN pid_started REAL")     # databases created before this column
        self._validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction; IMMEDIATE takes the write lock up front."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ events

    def _event(self, c: sqlite3.Connection, ticket_id: str | None, actor: str,
               type_: str, payload: dict[str, Any] | None = None) -> None:
        c.execute(
            "INSERT INTO events (ticket_id, ts, actor, type, payload) VALUES (?,?,?,?,?)",
            (ticket_id, _now(), actor, type_, json.dumps(payload or {})),
        )

    def add_event(self, ticket_id: str | None, actor: str, type_: str,
                  payload: dict[str, Any] | None = None) -> None:
        with self._tx() as c:
            self._event(c, ticket_id, actor, type_, payload)

    def last_event_ts(self) -> str | None:
        row = self._conn.execute("SELECT ts FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row["ts"] if row else None

    def events(self, ticket_id: str | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM events WHERE seq > ?", [since_seq]
        if ticket_id is not None:
            q += " AND ticket_id = ?"
            args.append(ticket_id)
        rows = self._conn.execute(q + " ORDER BY seq", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # ----------------------------------------------------------------- tickets

    def _validate(self, ticket: dict[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(ticket), key=lambda e: list(e.path))
        if errors:
            raise StoreError("; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors))

    def _load(self, c: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
        row = c.execute("SELECT data FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise NotFound(f"ticket {ticket_id}")
        return json.loads(row["data"])

    def _save(self, c: sqlite3.Connection, ticket: dict[str, Any]) -> None:
        ticket["updated_at"] = _now()
        self._validate(ticket)
        c.execute(
            "UPDATE tickets SET status=?, assignee=?, data=?, updated_at=? WHERE id=?",
            (ticket["status"], ticket["assignee"], json.dumps(ticket), ticket["updated_at"], ticket["id"]),
        )

    def create_ticket(self, fields: dict[str, Any], actor: str,
                      parent_id: str | None = None, max_children: int | None = None) -> dict[str, Any]:
        """Create a ticket. With parent_id, it becomes a child: the path trace
        is extended from the parent and the parent's children list is updated."""
        with self._tx() as c:
            ticket = dict(fields)
            ticket.setdefault("id", uuid.uuid4().hex[:12])
            ticket["status"] = "open"
            ticket["created_by"] = actor
            ticket.setdefault("cc", [])
            ticket.setdefault("children", [])
            ticket.setdefault("questions", [])
            ticket.setdefault("result", None)
            now = _now()
            ticket["created_at"] = ticket["updated_at"] = now

            if parent_id is not None:
                parent = self._load(c, parent_id)
                if max_children is not None and len(parent["children"]) >= max_children:
                    raise StoreError(f"ticket {parent_id} already has {max_children} children")
                ticket["parent_id"] = parent_id
                ticket["path_trace"] = [*parent["path_trace"], parent_id]
                parent["children"].append(ticket["id"])
                self._save(c, parent)
            else:
                ticket.setdefault("parent_id", None)
                ticket.setdefault("path_trace", [actor])

            self._validate(ticket)
            c.execute(
                "INSERT INTO tickets (id, status, assignee, created_by, parent_id, data, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ticket["id"], ticket["status"], ticket["assignee"], actor,
                 ticket["parent_id"], json.dumps(ticket), now, now),
            )
            self._event(c, ticket["id"], actor, "created",
                        {"assignee": ticket["assignee"], "parent_id": ticket["parent_id"]})
            return ticket

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return self._load(self._conn, ticket_id)

    def list_tickets(self, status: str | None = None, assignee: str | None = None,
                     parent_id: str | None = None) -> list[dict[str, Any]]:
        q, args, where = "SELECT data FROM tickets", [], []
        for col, val in (("status", status), ("assignee", assignee), ("parent_id", parent_id)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if where:
            q += " WHERE " + " AND ".join(where)
        return [json.loads(r["data"]) for r in self._conn.execute(q + " ORDER BY created_at", args)]

    def transition(self, ticket_id: str, new_status: str, actor: str, note: str = "",
                   result: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically move a ticket to new_status if the move is legal."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            old = t["status"]
            if new_status not in TRANSITIONS.get(old, set()):
                raise InvalidTransition(f"{ticket_id}: {old} -> {new_status}")
            t["status"] = new_status
            if new_status in ("in_progress", "done", "failed", "cancelled"):
                t.pop("escalation", None)
            if result is not None:
                t["result"] = result
            self._save(c, t)
            self._event(c, ticket_id, actor, "status_changed",
                        {"from": old, "to": new_status, "note": note})
            return t

    def append_context(self, ticket_id: str, actor: str, text: str) -> dict[str, Any]:
        """Add guidance to a ticket's context (e.g. from the top manager when retrying an escalation)."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            t["context"] = f"{t['context']}\n\n[Guidance from {actor}]: {text}"
            self._save(c, t)
            self._event(c, ticket_id, actor, "context_appended", {"text": text})
            return t

    def add_verification(self, ticket_id: str, actor: str, verdict: str, notes: list[str]) -> dict[str, Any]:
        """Attach advisory verification notes to a ticket and log the verdict."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            t.setdefault("verification_notes", []).extend(notes)
            self._save(c, t)
            self._event(c, ticket_id, actor, "verification", {"verdict": verdict, "notes": notes})
            return t

    def assign(self, ticket_id: str, assignee: str, actor: str) -> dict[str, Any]:
        with self._tx() as c:
            t = self._load(c, ticket_id)
            old, t["assignee"] = t["assignee"], assignee
            self._save(c, t)
            self._event(c, ticket_id, actor, "assigned", {"from": old, "to": assignee})
            return t

    def escalate(self, ticket_id: str, actor: str, reason: str, kind: str = "blocked",
                 to: str | None = None) -> dict[str, Any]:
        """Escalate a ticket and record who must handle it (see route_for). Kinds: blocked, technical,
        safety, refusal. Safety and refusal go straight to the human."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            old = t["status"]
            if "escalated" not in TRANSITIONS.get(old, set()):
                raise InvalidTransition(f"{ticket_id}: {old} -> escalated")
            to = to or route_for(t, kind)
            t["status"] = "escalated"
            t["escalation"] = {"to": to, "kind": kind, "reason": reason}
            self._save(c, t)
            self._event(c, ticket_id, actor, "status_changed",
                        {"from": old, "to": "escalated", "note": reason, "escalated_to": to, "kind": kind})
            return t

    def handoff(self, ticket_id: str, actor: str, reason: str = "") -> dict[str, Any]:
        """The level above could not resolve this: hand it to the human."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            if t["status"] not in ("escalated", "needs_clarification"):
                raise InvalidTransition(f"{ticket_id}: nothing to hand off ({t['status']})")
            esc = dict(t.get("escalation") or {"kind": "blocked", "reason": ""})
            esc.update(to="human", handed_off_by=actor)
            if reason:
                esc["reason"] = reason
            t["escalation"] = esc
            self._save(c, t)
            self._event(c, ticket_id, actor, "handoff", {"to": "human", "reason": reason})
            return t

    def ask_question(self, ticket_id: str, asked_by: str, question: str) -> dict[str, Any]:
        """A worker asks upward; the ticket moves to needs_clarification."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            if "needs_clarification" not in TRANSITIONS.get(t["status"], set()):
                raise InvalidTransition(f"{ticket_id}: cannot ask from {t['status']}")
            t["questions"].append({"asked_by": asked_by, "question": question})
            t["status"] = "needs_clarification"
            # a worker's question goes to its manager; a manager's goes to the top manager
            t["escalation"] = {"to": route_for(t, "question"), "kind": "question", "reason": question}
            self._save(c, t)
            self._event(c, ticket_id, asked_by, "question_asked", {"question": question})
            return t

    def answer_question(self, ticket_id: str, answered_by: str, answer: str) -> dict[str, Any]:
        """Answer the oldest unanswered question; resume work if none remain."""
        with self._tx() as c:
            t = self._load(c, ticket_id)
            pending = [q for q in t["questions"] if "answer" not in q]
            if not pending:
                raise StoreError(f"{ticket_id}: no unanswered question")
            pending[0].update(answered_by=answered_by, answer=answer)
            if len(pending) == 1 and t["status"] == "needs_clarification":
                t["status"] = "in_progress"
                t.pop("escalation", None)
            self._save(c, t)
            self._event(c, ticket_id, answered_by, "question_answered", {"answer": answer})
            return t

    # -------------------------------------------------------------------- jobs

    def create_job(self, ticket_id: str, actor: str, name: str, command: list[str], log_path: str,
                   resource: str | None = None, pid: int | None = None, job_id: str | None = None,
                   pid_started: float | None = None) -> dict[str, Any]:
        job_id = job_id or uuid.uuid4().hex[:10]
        with self._tx() as c:
            c.execute(
                "INSERT INTO jobs (id, ticket_id, actor, name, command, resource, pid, pid_started, log_path, started_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, ticket_id, actor, name, json.dumps(command), resource, pid, pid_started, log_path, _now()))
            self._event(c, ticket_id, actor, "job_started", {"job": job_id, "name": name, "resource": resource})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"job {job_id}")
        return {**dict(row), "command": json.loads(row["command"])}

    def update_job(self, job_id: str, actor: str, **fields: Any) -> dict[str, Any]:
        allowed = {"state", "exit_code", "ended_at", "pid"}
        if not fields or set(fields) - allowed:
            raise StoreError(f"update_job fields must be within {sorted(allowed)}")
        with self._tx() as c:
            sets = ", ".join(f"{k}=?" for k in fields)
            cur = c.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id])
            if cur.rowcount == 0:
                raise NotFound(f"job {job_id}")
            row = c.execute("SELECT ticket_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if "state" in fields:
                self._event(c, row["ticket_id"], actor, f"job_{fields['state']}",
                            {"job": job_id, "exit_code": fields.get("exit_code")})
        return self.get_job(job_id)

    def list_jobs(self, state: str | None = None, ticket_id: str | None = None) -> list[dict[str, Any]]:
        q, args, where = "SELECT * FROM jobs", [], []
        for col, val in (("state", state), ("ticket_id", ticket_id)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if where:
            q += " WHERE " + " AND ".join(where)
        return [{**dict(r), "command": json.loads(r["command"])}
                for r in self._conn.execute(q + " ORDER BY started_at", args)]

    # --------------------------------------------------------------- resources

    def register_resource(self, name: str, safe_to_stop: str = "") -> None:
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO resources (name, safe_to_stop) VALUES (?,?)",
                      (name, safe_to_stop))

    def claim_resource(self, name: str, ticket_id: str, expected_duration: str = "",
                       actor: str = "unknown") -> None:
        """Atomic claim: succeeds only if the resource is free."""
        with self._tx() as c:
            now = _now()
            cur = c.execute(
                "UPDATE resources SET state='busy', holder_ticket=?, expected_duration=?,"
                " started_at=?, last_heartbeat=? WHERE name=? AND state='free'",
                (ticket_id, expected_duration, now, now, name),
            )
            if cur.rowcount == 0:
                row = c.execute("SELECT state, holder_ticket FROM resources WHERE name=?", (name,)).fetchone()
                if row is None:
                    raise NotFound(f"resource {name}")
                raise ResourceBusy(f"{name} is {row['state']} (held by {row['holder_ticket']})")
            self._event(c, ticket_id, actor, "resource_claimed", {"resource": name})

    def release_resource(self, name: str, ticket_id: str, actor: str = "unknown") -> None:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE resources SET state='free', holder_ticket=NULL, expected_duration=NULL,"
                " started_at=NULL, last_heartbeat=NULL WHERE name=? AND holder_ticket=?",
                (name, ticket_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"{ticket_id} does not hold {name}")
            self._event(c, ticket_id, actor, "resource_released", {"resource": name})

    def force_release_resource(self, name: str, actor: str, reason: str) -> None:
        """Free a resource regardless of holder. For recovery after a crash or power loss, once a human
        has confirmed the hardware's physical state."""
        with self._tx() as c:
            row = c.execute("SELECT holder_ticket FROM resources WHERE name=?", (name,)).fetchone()
            if row is None:
                raise NotFound(f"resource {name}")
            c.execute("UPDATE resources SET state='free', holder_ticket=NULL, expected_duration=NULL,"
                      " started_at=NULL, last_heartbeat=NULL WHERE name=?", (name,))
            self._event(c, row["holder_ticket"], actor, "resource_force_released", {"resource": name, "reason": reason})

    def heartbeat_resource(self, name: str, ticket_id: str) -> None:
        with self._tx() as c:
            cur = c.execute("UPDATE resources SET last_heartbeat=? WHERE name=? AND holder_ticket=?",
                            (_now(), name, ticket_id))
            if cur.rowcount == 0:
                raise StoreError(f"{ticket_id} does not hold {name}")

    def set_safe_to_stop(self, name: str, note: str, actor: str = "experiment_manager") -> None:
        with self._tx() as c:
            cur = c.execute("UPDATE resources SET safe_to_stop=? WHERE name=?", (note, name))
            if cur.rowcount == 0:
                raise NotFound(f"resource {name}")
            self._event(c, None, actor, "safe_to_stop_updated", {"resource": name, "note": note})

    def list_resources(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM resources ORDER BY name")]
