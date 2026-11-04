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
import threading
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .hardware import JobRunner, python_exe, resolve_in_workspace, venv_python
from .store import Store, StoreError
from .util import atomic_write

PIP_SPEC_RE = re.compile(r"^[A-Za-z0-9]\S*$")  # no shell is used; this blocks option injection and spaces
PROTECTED = ("memory", "orchestrator.db", "PAUSE", "jobs")
SECRET_FILE_RE = re.compile(r"(^\.env(\..*)?$)|(\.(pem|key|pfx|p12)$)|(^id_(rsa|ed25519|ecdsa)|^credentials|^secrets?\.)", re.I)
SECRET_ALLOWED = {".env.example", ".env.sample", ".env.template"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "UNLICENSE")


class ToolError(Exception):
    pass


_REGISTRY_LOCK = threading.Lock()      # parallel workers may register tools at the same moment


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
        with _REGISTRY_LOCK:
            tools = [t for t in self.list() if t["name"] != entry["name"]]
            tools.append(entry)
            atomic_write(self.path, json.dumps(sorted(tools, key=lambda t: t["name"]), indent=2))
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


def _default_run(args: list[str], timeout: float = 60, cwd: str | Path | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out, f"timed out after {timeout}s"


class ToolBox:
    def __init__(self, workspace: str | Path, actor: str, can_acquire: bool = False,
                 store: Store | None = None, tooling_assignee: str = "tooling_manager",
                 http_get: Callable[[str], Any] = _default_http_get,
                 git: Callable[..., str] = _default_git, allow_local_git: bool = False,
                 ticket_id: str | None = None, can_use_hardware: bool = False,
                 jobs: JobRunner | None = None,
                 run_cmd: Callable[..., tuple[int, str, str]] = _default_run,
                 read_roots: list[str] | None = None, departments: dict[str, str] | None = None) -> None:
        self.workspace = Path(workspace)
        self.departments = dict(departments or {})      # department name -> its manager, for request_work
        self.read_roots = [Path(r).expanduser().resolve() for r in (read_roots or [])]
        self.registry = ToolRegistry(workspace)
        self.actor, self.can_acquire, self.store = actor, can_acquire, store
        self.tooling_assignee = tooling_assignee
        self.http_get, self.git, self.allow_local_git = http_get, git, allow_local_git
        self.ticket_id, self.can_use_hardware, self.run_cmd = ticket_id, can_use_hardware, run_cmd
        self.jobs = jobs if jobs is not None else (JobRunner(store, workspace) if store else None)

    def describe(self) -> str:
        lines = ["Tools (reply {\"action\": \"tool\", \"tool\": \"<name>\", \"args\": {...}}; the result comes back to you):",
                 "- search_registry {query}: find existing tools. ALWAYS try this before building or requesting anything.",
                 "- request_tool {title, need, acceptance_criteria?}: ask the Tooling department for a tool."]
        if self.departments:
            lines.append("- request_work {department, title, need, deliver?}: file work for another department ("
                         + ", ".join(sorted(self.departments)) + "). It runs in parallel and the top manager sees it. Use it "
                         "when you find work that should be done and is not part of your ticket: speak up.")
        lines += ["- read_file {path} / write_file {path, content}: files inside the workspace (relative paths).",
                  "- run_python {script, args?, timeout?}: run a workspace Python script now (tool venv if present) and get its output.",
                  "- list_dir {path}: list a directory (workspace-relative, or absolute under a read root)."]
        if self.read_roots:
            lines.append("  Read-only project folders you may study with list_dir and read_file (absolute paths): "
                         + ", ".join(str(r) for r in self.read_roots) + ". You cannot write there; save your output in the workspace. Secret files are blocked.")
        if self.can_use_hardware and self.jobs:
            lines += ["Hardware (Experiment department). Prefer an automated Python tool that runs the whole sweep over driving hardware by hand; then oversee it:",
                      "- list_resources {}: hardware flags. claim_resource {name, expected_duration} BEFORE touching a resource; release_resource {name} when done.",
                      "- start_job {script, args?, name?, resource?}: run a long script in the background (needs the resource claimed). job_status {job_id}, job_tail {job_id, lines?}, wait_job {job_id, seconds?}, stop_job {job_id}.",
                      "- heartbeat_resource {name}: call periodically while you hold a resource.",
                      "If anything looks physically unsafe, stop the job and reply with the escalate action."]
        if self.can_acquire:
            lines += ["- pypi_info {name}: look up a PyPI package (summary, version, license, requirements).",
                      "- pip_install {spec, name?, description?, tags?, usage?}: install a PyPI package (or any pip spec, including git+https URLs) into the workspace tool venv and register it. Any Python source is fair game; check licenses.","- search_github {query}: find open-source candidates (name, stars, license, url).",
                      "- acquire_git {url, name, description, tags?, usage?}: shallow-clone an https repo into the tool library and register it.",
                      "- register_tool {name, description, kind, path?, usage?, tags?}: register a tool you wrote or located.",
                      "Prefer mature, maintained, permissively licensed open-source tools over writing your own. "
                      "Check the license and note it. Do not run acquired code you have not inspected."]
        return "\n".join(lines)

    ACQUIRE_ONLY = {"search_github", "acquire_git", "register_tool", "pypi_info", "pip_install"}
    HARDWARE_ONLY = {"claim_resource", "release_resource", "heartbeat_resource", "start_job", "stop_job"}

    def has_tool(self, name: Any) -> bool:
        return isinstance(name, str) and bool(name) and hasattr(self, f"_t_{name}")

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            fn = getattr(self, f"_t_{tool}", None)
            if fn is None:
                raise ToolError(f"unknown tool: {tool}")
            if tool in self.ACQUIRE_ONLY and not self.can_acquire:
                raise ToolError(f"{tool} is restricted to the Tooling department; use request_tool")
            if tool in self.HARDWARE_ONLY and not self.can_use_hardware:
                raise ToolError(f"{tool} is restricted to the Experiment department; request an experiment ticket")
            return {"ok": True, **fn(args or {})}
        except (ToolError, StoreError, OSError, ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------- tools

    def _t_search_registry(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"results": self.registry.search(str(a.get("query", "")))}

    def _t_request_work(self, a: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            raise ToolError("ticketing unavailable")
        dept = str(a.get("department", "")).strip()
        if dept not in self.departments:
            raise ToolError(f"unknown department {dept!r}; choose from {sorted(self.departments)}")
        t = self.store.create_ticket({
            "title": str(a["title"])[:200], "assignee": self.departments[dept], "worker_role": "worker", "cc": ["top_manager"],
            "context": f"Requested by {self.actor} (while working on ticket {self.ticket_id}): {a['need']}",
            "acceptance_criteria": a.get("acceptance_criteria") or ["the requester's need is met"],
            "return_spec": {"format": "summary", "description": str(a.get("deliver") or "what was done and where to find it")},
            "rigor": a.get("rigor", "balanced")}, actor=self.actor)
        return {"ticket_id": t["id"], "department": dept,
                "note": "it runs in parallel and is not part of your ticket: carry on with your own work"}

    def _t_request_tool(self, a: dict[str, Any]) -> dict[str, Any]:
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

    def _t_search_github(self, a: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.quote(str(a["query"]))
        data = self.http_get(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=8")
        return {"results": [{
            "name": i["full_name"], "description": i.get("description"), "stars": i.get("stargazers_count"),
            "license": (i.get("license") or {}).get("spdx_id"), "url": i["html_url"],
            "updated": i.get("pushed_at"), "archived": i.get("archived", False)} for i in data.get("items", [])]}

    def _t_acquire_git(self, a: dict[str, Any]) -> dict[str, Any]:
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

    def _t_register_tool(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"registered": self.registry.register({**a, "added_by": self.actor})}

    # ------------------------------------------------------- files and Python

    def _safe_path(self, rel: str, write: bool = False) -> Path:
        try:
            p = resolve_in_workspace(self.workspace, str(rel))
        except StoreError as e:
            raise ToolError(str(e)) from e
        if write:
            parts = p.relative_to(self.workspace.resolve()).parts
            if parts and any(parts[0] == x or parts[0].startswith(x) for x in PROTECTED):
                raise ToolError(f"{parts[0]} is managed by the orchestrator and cannot be written")
        return p

    def _read_path(self, raw: str) -> Path:
        """A path to READ: relative paths are inside the workspace; absolute paths must be inside a configured
        read root (a project repo the workers may study but not change). Secret files are never readable."""
        if not Path(str(raw)).is_absolute():
            p = self._safe_path(raw)
        else:
            p = Path(str(raw)).expanduser().resolve()
            if not any(r == p or r in p.parents for r in self.read_roots + [self.workspace.resolve()]):
                raise ToolError(f"{raw} is outside the workspace and the configured read roots")
        if p.name.lower() not in SECRET_ALLOWED and SECRET_FILE_RE.search(p.name):
            raise ToolError(f"{p.name} looks like a secrets file and is never readable by agents")
        return p

    def _t_list_dir(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self._read_path(str(a.get("path", ".")))
        if not p.is_dir():
            raise ToolError(f"not a directory: {a.get('path')}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))[:300]:
            hidden = child.is_file() and child.name.lower() not in SECRET_ALLOWED and SECRET_FILE_RE.search(child.name)
            if hidden or child.name == ".git":
                continue
            entries.append({"name": child.name + ("/" if child.is_dir() else ""),
                            "bytes": child.stat().st_size if child.is_file() else None})
        return {"path": str(p), "entries": entries}

    def _t_read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self._read_path(a["path"])
        if not p.is_file():
            raise ToolError(f"not a file: {a['path']}")
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"content": text[:20000], "truncated": len(text) > 20000}

    def _t_write_file(self, a: dict[str, Any]) -> dict[str, Any]:
        content = str(a["content"])
        if len(content) > 200_000:
            raise ToolError("file too large (200KB max)")
        p = self._safe_path(a["path"], write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": str(a["path"]), "bytes": len(content.encode("utf-8"))}

    def _t_run_python(self, a: dict[str, Any]) -> dict[str, Any]:
        script = self._safe_path(a["script"])
        if not script.is_file():
            raise ToolError(f"script not found: {a['script']}")
        timeout = min(float(a.get("timeout", 60)), 300)
        rc, out, err = self.run_cmd([python_exe(self.workspace), str(script), *[str(x) for x in a.get("args", [])]],
                                    timeout=timeout, cwd=self.workspace)
        return {"exit_code": rc, "stdout": out[-4000:], "stderr": err[-4000:]}

    # ------------------------------------------------------- Python sourcing

    def _t_pypi_info(self, a: dict[str, Any]) -> dict[str, Any]:
        name = str(a["name"])
        if not NAME_RE.match(name):
            raise ToolError("invalid package name")
        info = self.http_get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json").get("info", {})
        return {"name": info.get("name"), "version": info.get("version"), "summary": info.get("summary"),
                "license": info.get("license") or info.get("license_expression"),
                "requires_python": info.get("requires_python"), "home": info.get("home_page") or info.get("project_urls"),
                "requires": (info.get("requires_dist") or [])[:15]}

    def _t_pip_install(self, a: dict[str, Any]) -> dict[str, Any]:
        spec = str(a["spec"])
        if not PIP_SPEC_RE.match(spec) or spec.startswith("-"):
            raise ToolError("invalid pip spec (no options or shell characters)")
        if venv_python(self.workspace) is None:
            rc, _, err = self.run_cmd([sys.executable, "-m", "venv", str(self.workspace / "tools" / "venv")], timeout=300)
            if rc != 0:
                raise ToolError(f"could not create tool venv: {err[-300:]}")
        py = venv_python(self.workspace)
        if py is None:
            raise ToolError("tool venv has no python executable")
        rc, out, err = self.run_cmd([str(py), "-m", "pip", "install", spec], timeout=600)
        if rc != 0:
            raise ToolError(f"pip install failed: {(err or out)[-500:]}")
        base = re.split(r"[\[=<>!~@ ]", spec.rsplit("/", 1)[-1].removesuffix(".git"))[0] or spec
        name = str(a.get("name") or base)
        entry = self.registry.register({
            "name": name, "description": str(a.get("description") or f"pip package {spec}"), "kind": "pip",
            "source_url": spec, "tags": a.get("tags") or [], "usage": str(a.get("usage", f"import {base.replace('-', '_')}")),
            "added_by": self.actor})
        return {"registered": entry, "pip_output_tail": out[-300:]}

    # ---------------------------------------------------------------- hardware

    def _need_store(self) -> Store:
        if self.store is None or self.ticket_id is None:
            raise ToolError("hardware tools need a ticket context")
        return self.store

    def _t_list_resources(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"resources": self._need_store().list_resources(),
                "running_jobs": [{k: j[k] for k in ("id", "name", "resource", "ticket_id")}
                                 for j in self._need_store().list_jobs(state="running")]}

    def _t_claim_resource(self, a: dict[str, Any]) -> dict[str, Any]:
        self._need_store().claim_resource(str(a["name"]), self.ticket_id, str(a.get("expected_duration", "")), self.actor)
        return {"claimed": a["name"]}

    def _t_release_resource(self, a: dict[str, Any]) -> dict[str, Any]:
        running = [j for j in self._need_store().list_jobs(state="running", ticket_id=self.ticket_id)
                   if j["resource"] == a["name"]]
        if running and self.jobs:
            running = [j for j in (self.jobs.refresh(j["id"]) for j in running) if j["state"] == "running"]
        if running:
            raise ToolError(f"job {running[0]['id']} is still using {a['name']}; stop it or wait for it first")
        self._need_store().release_resource(str(a["name"]), self.ticket_id, self.actor)
        return {"released": a["name"]}

    def _t_heartbeat_resource(self, a: dict[str, Any]) -> dict[str, Any]:
        self._need_store().heartbeat_resource(str(a["name"]), self.ticket_id)
        return {"ok_until_next_heartbeat": True}

    def _runner(self) -> JobRunner:
        self._need_store()
        if self.jobs is None:
            raise ToolError("job runner unavailable")
        return self.jobs

    def _t_start_job(self, a: dict[str, Any]) -> dict[str, Any]:
        j = self._runner().start(self.ticket_id, self.actor, str(a["script"]), a.get("args") or [],
                                 a.get("name"), a.get("resource"))
        return {"job_id": j["id"], "pid": j["pid"], "log": j["log_path"]}

    def _t_job_status(self, a: dict[str, Any]) -> dict[str, Any]:
        j = self._runner().refresh(str(a["job_id"]), self.actor)
        return {"state": j["state"], "exit_code": j["exit_code"], "name": j["name"], "resource": j["resource"]}

    def _t_job_tail(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"log": self._runner().tail(str(a["job_id"]), int(a.get("lines", 40)))}

    def _t_wait_job(self, a: dict[str, Any]) -> dict[str, Any]:
        j = self._runner().wait(str(a["job_id"]), float(a.get("seconds", 30)))
        return {"state": j["state"], "exit_code": j["exit_code"], "log_tail": self._runner().tail(j["id"], 10)}

    def _t_stop_job(self, a: dict[str, Any]) -> dict[str, Any]:
        j = self._runner().stop(str(a["job_id"]), self.actor)
        return {"state": j["state"]}
