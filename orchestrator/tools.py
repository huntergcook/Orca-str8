"""Tool registry and the ToolBox workers call into.

Anti-reinvention rule: check the registry, then open source, then build.

  search_registry   (every worker)   find tools we already have
  request_tool      (every worker)   file a ticket to Tooling, top manager copied
  search_github     (Tooling only)   find open-source candidates
  acquire_git       (Tooling only)   shallow-clone a repo into tools/vendor/<name> and register it
  register_tool     (Tooling only)   record a tool we wrote or a vendored one

Acquired code is NEVER executed by this module. Cloned repos are untrusted until a worker
inspects them; there is no sandbox yet (single-machine mode).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .store import Store

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "UNLICENSE")


class ToolError(Exception):
    pass


class ToolRegistry:
    def __init__(self, workspace: str | Path) -> None:
        self.root = Path(workspace) / "tools"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "registry.json"

    def list(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def get(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.list() if t["name"] == name), None)

    def register(self, entry: dict[str, Any]) -> dict[str, Any]:
        for k in ("name", "description", "kind"):
            if not entry.get(k):
                raise ToolError(f"missing field: {k}")
        if not NAME_RE.match(entry["name"]):
            raise ToolError("invalid tool name")
        entry = {"tags": [], "usage": "", "source_url": None, "license": None, "path": None,
                 "added_by": "unknown", **entry}
        tools = [t for t in self.list() if t["name"] != entry["name"]]
        tools.append(entry)
        self.path.write_text(json.dumps(sorted(tools, key=lambda t: t["name"]), indent=2), encoding="utf-8")
        return entry

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        scored = []
        for t in self.list():
            hay_name, hay = t["name"].lower(), " ".join(
                [t["description"], t.get("usage", ""), " ".join(t.get("tags", []))]).lower()
            score = sum(3 for w in words if w in hay_name) + sum(1 for w in words if w in hay)
            if score:
                scored.append((score, t))
        return [t for _, t in sorted(scored, key=lambda x: -x[0])[:limit]]


def _default_http_get(url: str) -> Any:
    headers = {"User-Agent": "orchestrator-tooling", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
        return json.load(r)


def _default_git(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise ToolError(f"git failed: {r.stderr.strip()[:300]}")
    return r.stdout


class ToolBox:
    def __init__(self, workspace: str | Path, actor: str, can_acquire: bool = False,
                 store: Store | None = None, tooling_assignee: str = "tooling_manager",
                 http_get: Callable[[str], Any] = _default_http_get,
                 git: Callable[..., str] = _default_git, allow_local_git: bool = False) -> None:
        self.registry = ToolRegistry(workspace)
        self.actor, self.can_acquire, self.store = actor, can_acquire, store
        self.tooling_assignee = tooling_assignee
        self.http_get, self.git, self.allow_local_git = http_get, git, allow_local_git

    def describe(self) -> str:
        lines = ["Tools (reply {\"action\": \"tool\", \"tool\": \"<name>\", \"args\": {...}}; the result comes back to you):",
                 "- search_registry {query}: find existing tools. ALWAYS try this before building or requesting anything.",
                 "- request_tool {title, need, acceptance_criteria?}: ask the Tooling department for a tool."]
        if self.can_acquire:
            lines += ["- search_github {query}: find open-source candidates (name, stars, license, url).",
                      "- acquire_git {url, name, description, tags?, usage?}: shallow-clone an https repo into the tool library and register it.",
                      "- register_tool {name, description, kind, path?, usage?, tags?}: register a tool you wrote or located.",
                      "Prefer mature, maintained, permissively licensed open-source tools over writing your own. "
                      "Check the license and note it. Do not run acquired code you have not inspected."]
        return "\n".join(lines)

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            fn = {"search_registry": self._search_registry, "request_tool": self._request_tool,
                  "search_github": self._search_github, "acquire_git": self._acquire_git,
                  "register_tool": self._register_tool}.get(tool)
            if fn is None:
                raise ToolError(f"unknown tool: {tool}")
            if tool in ("search_github", "acquire_git", "register_tool") and not self.can_acquire:
                raise ToolError(f"{tool} is restricted to the Tooling department; use request_tool")
            return {"ok": True, **fn(args or {})}
        except (ToolError, OSError, ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------- tools

    def _search_registry(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"results": self.registry.search(str(a.get("query", "")))}

    def _request_tool(self, a: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            raise ToolError("ticketing unavailable")
        existing = self.registry.search(f"{a.get('title', '')} {a.get('need', '')}", limit=3)
        t = self.store.create_ticket({
            "title": str(a["title"]), "assignee": self.tooling_assignee, "worker_role": "tool_scout",
            "cc": ["top_manager"], "context": str(a["need"]),
            "acceptance_criteria": a.get("acceptance_criteria") or ["tool is registered and its usage is documented"],
            "return_spec": {"format": "registry entry", "description": "name of the registered tool and how to use it"},
        }, actor=self.actor)
        return {"ticket_id": t["id"], "possibly_relevant_existing": existing}

    def _search_github(self, a: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.quote(str(a["query"]))
        data = self.http_get(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=8")
        return {"results": [{
            "name": i["full_name"], "description": i.get("description"), "stars": i.get("stargazers_count"),
            "license": (i.get("license") or {}).get("spdx_id"), "url": i["html_url"],
            "updated": i.get("pushed_at"), "archived": i.get("archived", False)} for i in data.get("items", [])]}

    def _acquire_git(self, a: dict[str, Any]) -> dict[str, Any]:
        url, name = str(a["url"]), str(a["name"])
        if not NAME_RE.match(name):
            raise ToolError("invalid tool name")
        if not (url.startswith("https://") or (self.allow_local_git and Path(url).exists())):
            raise ToolError("only https:// git URLs are allowed")
        dest = self.registry.root / "vendor" / name
        if dest.exists():
            raise ToolError(f"{name} already vendored; search_registry first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git(["clone", "--depth", "1", "--no-tags", url, str(dest)])
        lic = next((f for f in LICENSE_FILES if (dest / f).exists()), None)
        license_head = (dest / lic).read_text(encoding="utf-8", errors="replace")[:200].strip() if lic else None
        entry = self.registry.register({
            "name": name, "description": str(a.get("description") or name), "kind": "vendored",
            "path": str(dest), "source_url": url, "license": license_head or "NO LICENSE FILE FOUND",
            "tags": a.get("tags") or [], "usage": str(a.get("usage", "")), "added_by": self.actor})
        return {"registered": entry, "warning": None if lic else "no license file; confirm terms before using"}

    def _register_tool(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"registered": self.registry.register({**a, "added_by": self.actor})}
