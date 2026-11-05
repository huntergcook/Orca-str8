"""Reaching the human when they are away. Every notification is appended to workspace/notifications.log (so nothing is
lost if no channel is configured), then sent to each configured channel. Channels use only the standard library, so
they keep working when other packages are broken, and a failing channel never stops the harness.

    notify:
      ntfy:    https://ntfy.sh/your-private-topic      # phone push; or just a topic name (uses ntfy.sh)
      webhook: https://hooks.example.com/...          # JSON POST {"level","title","body"} (Slack/Discord-compatible text too)
      command: "powershell -File notify.ps1"          # run with ORCA_LEVEL / ORCA_TITLE / ORCA_BODY in the environment
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .util import append_durable

LEVELS = ("info", "question", "blocked", "done", "error")


class Notifier:
    def __init__(self, cfg: dict[str, Any], workspace: str | Path,
                 post: Callable[[str, bytes, dict[str, str]], None] | None = None,
                 run: Callable[[str, dict[str, str]], None] | None = None) -> None:
        self.cfg = cfg.get("notify") or {}
        self.log = Path(workspace) / "notifications.log"
        self._post = post or self._default_post
        self._run = run or self._default_run
        self._last: dict[str, float] = {}

    @staticmethod
    def _default_post(url: str, body: bytes, headers: dict[str, str]) -> None:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=15).read()

    @staticmethod
    def _default_run(command: str, env: dict[str, str]) -> None:
        subprocess.run(command, shell=True, env={**os.environ, **env}, timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def __call__(self, level: str, title: str, body: str = "", key: str | None = None, min_gap: float = 0.0) -> bool:
        """Send one notification. `key` + `min_gap` (seconds) suppress repeats of the same alert."""
        import time
        now = time.monotonic()
        if key and min_gap and now - self._last.get(key, -1e18) < min_gap:
            return False
        if key:
            self._last[key] = now
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            append_durable(self.log, f"{ts} [{level}] {title}\n{body.strip()}\n\n")
        except OSError:
            pass
        sent = False
        ntfy = self.cfg.get("ntfy")
        if ntfy:
            url = ntfy if "://" in str(ntfy) else f"https://ntfy.sh/{ntfy}"
            sent |= self._try(lambda: self._post(url, body.encode("utf-8")[:3000] or b"-",
                                                 {"Title": title[:200].encode("ascii", "replace").decode(),
                                                  "Priority": "high" if level in ("blocked", "error", "question") else "default"}))
        hook = self.cfg.get("webhook")
        if hook:
            payload = json.dumps({"level": level, "title": title, "body": body, "text": f"{title}\n{body}"[:3000]}).encode()
            sent |= self._try(lambda: self._post(hook, payload, {"Content-Type": "application/json"}))
        cmd = self.cfg.get("command")
        if cmd:
            sent |= self._try(lambda: self._run(str(cmd), {"ORCA_LEVEL": level, "ORCA_TITLE": title, "ORCA_BODY": body}))
        return sent

    @staticmethod
    def _try(fn: Callable[[], None]) -> bool:
        try:
            fn()
            return True
        except Exception:
            return False
