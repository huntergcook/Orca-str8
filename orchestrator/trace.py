"""Tracing: every audit event is also exported as an OpenTelemetry span (OTLP/JSON), so standard tools (Langfuse, Phoenix,
Laminar, Jaeger) can show a per-ticket trace tree for drill-down while the dashboard stays the one-screen summary.

    trace:
      file: true                                  # workspace/traces.jsonl, one OTLP ResourceSpans object per line
      otlp_endpoint: http://localhost:4318        # optional; spans are POSTed to <endpoint>/v1/traces in the background
      headers: {Authorization: "Basic ..."}       # optional

Events are instantaneous, so each span has zero duration; ticket lifecycle shows up as the sequence of spans in the trace.
Failures never affect the run.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import append_durable


def _hex(text: str, n: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def to_span(event: dict[str, Any], root_of: str | None) -> dict[str, Any]:
    ts = datetime.fromisoformat(event["ts"])
    nano = str(int(ts.timestamp() * 1_000_000_000))
    trace_key = root_of or event.get("ticket_id") or "project"
    attrs = {"orca.actor": event["actor"], "orca.event": event["type"], "orca.ticket": event.get("ticket_id") or "",
             "orca.seq": event["seq"]}
    for k, v in (event.get("payload") or {}).items():
        if isinstance(v, (str, int, float, bool)):
            attrs[f"orca.{k}"] = v if not isinstance(v, str) else v[:300]
    return {"traceId": _hex(trace_key, 32), "spanId": _hex(f"{event['seq']}", 16), "name": f"{event['actor']}:{event['type']}",
            "kind": 1, "startTimeUnixNano": nano, "endTimeUnixNano": nano,
            "attributes": [{"key": k, "value": ({"intValue": str(v)} if isinstance(v, int) and not isinstance(v, bool)
                                                 else {"boolValue": v} if isinstance(v, bool)
                                                 else {"doubleValue": v} if isinstance(v, float) else {"stringValue": str(v)})}
                           for k, v in attrs.items()]}


class Tracer:
    def __init__(self, cfg: dict[str, Any], workspace: str | Path) -> None:
        t = cfg.get("trace") or {}
        self.file = Path(workspace) / "traces.jsonl" if t.get("file", False) else None
        self.endpoint = (str(t["otlp_endpoint"]).rstrip("/") + "/v1/traces") if t.get("otlp_endpoint") else None
        self.headers = {"Content-Type": "application/json", **{str(k): str(v) for k, v in (t.get("headers") or {}).items()}}
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=5000)
        self.enabled = bool(self.file or self.endpoint)
        if self.endpoint:
            threading.Thread(target=self._pump, daemon=True, name="otlp-export").start()

    def emit(self, event: dict[str, Any], root_of: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            span = to_span(event, root_of)
            payload = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "orca"}}]},
                                          "scopeSpans": [{"scope": {"name": "orchestrator"}, "spans": [span]}]}]}
            body = json.dumps(payload).encode("utf-8")
            if self.file:
                append_durable(self.file, body.decode("utf-8") + "\n")
            if self.endpoint:
                self._q.put_nowait(body)
        except Exception:
            pass

    def _pump(self) -> None:
        while True:
            body = self._q.get()
            try:
                urllib.request.urlopen(urllib.request.Request(self.endpoint, data=body, headers=self.headers, method="POST"),
                                       timeout=10).read()
            except Exception:
                pass
