"""Code-writing pipeline: isolated git worktrees, scoped writes, automatic gates, review packets, merge.

A code ticket carries a `code` spec:

    {"allowed_paths": ["harness/*.py", "tests/test_x.py"],      # where the worker may write (globs)
     "acceptance":    ["python -m pytest -q tests/test_x.py"],  # commands that must all exit 0 in the worktree
     "context_files": ["harness/protocol.py"]}                  # files shown to the worker up front

The worker edits files in its own worktree (branch ticket/<id>) with a small tool set. When it says it is done,
the orchestrator commits, runs the gates and, if any fail, gives the failures back to the SAME conversation to fix.
A passing branch is NOT merged: a human or the top manager reviews it (`ctl code show`) and accepts or rejects it.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import SECRET_ALLOWED, SECRET_FILE_RE, ToolBox, ToolError

GIT_ID = ["-c", "user.name=orchestrator", "-c", "user.email=orchestrator@localhost"]
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api|secret|access)[_-]?(?:key|token)\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"),
]
CODE_TOOLS = ("read_file", "list_dir", "write_file", "edit_file", "search_text", "run_tests")


class CodeError(Exception):
    pass


# ----------------------------------------------------------------------------- globs

def _glob_re(pattern: str) -> re.Pattern[str]:
    pat, i, out = pattern.replace("\\", "/"), 0, ""
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat[i:i + 2] == "**":
                i += 2
                if pat[i:i + 1] == "/":
                    i += 1
                    out += "(?:.*/)?"            # '**/' = zero or more directories
                else:
                    out += ".*"
                continue
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        else:
            out += re.escape(ch)
        i += 1
    return re.compile("^" + out + "$")


def glob_match(path: str, pattern: str) -> bool:
    """'*' stays inside one directory, '**' crosses directories, '?' is one character."""
    return bool(_glob_re(pattern).match(path.replace("\\", "/")))


# -------------------------------------------------------------------------------- git

def _git(args: list[str], cwd: Path | str, check: bool = True, timeout: float = 300) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise CodeError(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


@dataclass
class CodeSpec:
    allowed_paths: list[str]
    acceptance: list[str]
    context_files: list[str] = field(default_factory=list)

    @classmethod
    def from_ticket(cls, ticket: dict[str, Any]) -> "CodeSpec":
        c = ticket.get("code") or {}
        return cls(list(c.get("allowed_paths") or []), list(c.get("acceptance") or []),
                   list(c.get("context_files") or []))

    @staticmethod
    def problems(code: Any) -> list[str]:
        """What is wrong with a code spec; empty if it is usable."""
        if not isinstance(code, dict):
            return ["'code' must be an object with allowed_paths and acceptance"]
        out = []
        if not code.get("allowed_paths"):
            out.append("allowed_paths is empty (narrow globs, at most about 3 files)")
        if not code.get("acceptance"):
            out.append("acceptance is empty (at least one command that fails before and passes after)")
        if any(a.strip() in ("**", "*", "**/*") for a in code.get("allowed_paths") or []):
            out.append("allowed_paths is too broad")
        return out


class CodeRepo:
    """The git repository code tickets change, with one worktree and branch per ticket."""

    def __init__(self, repo: str | Path, base_branch: str | None = None, worktrees_dir: str | Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        _git(["rev-parse", "--git-dir"], self.repo)                     # raises CodeError if not a repository
        self.base_branch = base_branch or _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip()
        self.worktrees_dir = Path(worktrees_dir).resolve() if worktrees_dir else self.repo.parent / f"{self.repo.name}-worktrees"

    # -------------------------------------------------------------- worktrees
    def worktree_path(self, ticket_id: str) -> Path:
        return self.worktrees_dir / ticket_id

    def branch(self, ticket_id: str) -> str:
        return f"ticket/{ticket_id}"

    def has_worktree(self, ticket_id: str) -> bool:
        return self.worktree_path(ticket_id).exists()

    def ensure_worktree(self, ticket_id: str) -> Path:
        """The ticket's worktree, created from the base branch's current commit if it does not exist yet."""
        path = self.worktree_path(ticket_id)
        if path.exists():
            return path
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        exists = bool(_git(["branch", "--list", self.branch(ticket_id)], self.repo).strip())
        args = ["worktree", "add", str(path), self.branch(ticket_id)] if exists else \
            ["worktree", "add", "-b", self.branch(ticket_id), str(path), self.base_branch]
        _git(args, self.repo)
        return path

    def discard(self, ticket_id: str) -> None:
        path = self.worktree_path(ticket_id)
        if path.exists():
            _git(["worktree", "remove", "--force", str(path)], self.repo, check=False)
        _git(["worktree", "prune"], self.repo, check=False)
        _git(["branch", "-D", self.branch(ticket_id)], self.repo, check=False)

    # ---------------------------------------------------------------- changes
    def commit_all(self, ticket_id: str, message: str) -> str | None:
        wt = self.worktree_path(ticket_id)
        _git(["add", "-A"], wt)
        if not _git(["status", "--porcelain"], wt).strip():
            return None
        _git([*GIT_ID, "commit", "-q", "-m", message], wt)
        return _git(["rev-parse", "--short", "HEAD"], wt).strip()

    def _merge_base(self, ticket_id: str) -> str:
        return _git(["merge-base", self.base_branch, self.branch(ticket_id)], self.repo).strip()

    def changed_files(self, ticket_id: str) -> list[tuple[str, str]]:
        out = _git(["diff", "--name-status", self._merge_base(ticket_id), self.branch(ticket_id)], self.repo)
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0][0], parts[-1]))
        return rows

    def diff(self, ticket_id: str, max_lines: int = 300) -> str:
        text = _git(["diff", self._merge_base(ticket_id), self.branch(ticket_id)], self.repo)
        lines = text.splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... diff truncated: {len(lines) - max_lines} more lines"
        return text

    def diffstat(self, ticket_id: str) -> str:
        return _git(["diff", "--stat", self._merge_base(ticket_id), self.branch(ticket_id)], self.repo).strip()

    def added_lines(self, ticket_id: str) -> list[str]:
        text = _git(["diff", "-U0", self._merge_base(ticket_id), self.branch(ticket_id)], self.repo)
        return [l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]

    # ------------------------------------------------------------------ merge
    def merge(self, ticket_id: str, message: str) -> str:
        """Merge the ticket's branch into the base branch (no fast-forward), then clean up. Refuses if the
        repository is on another branch or has uncommitted tracked changes; aborts cleanly on conflict."""
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip()
        if current != self.base_branch:
            raise CodeError(f"the repository is on '{current}', not '{self.base_branch}'; switch back before accepting")
        if _git(["status", "--porcelain", "--untracked-files=no"], self.repo).strip():
            raise CodeError("the repository has uncommitted changes to tracked files; commit or stash them first")
        r = subprocess.run(["git", *GIT_ID, "merge", "--no-ff", "-m", message, self.branch(ticket_id)], cwd=str(self.repo),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            _git(["merge", "--abort"], self.repo, check=False)
            raise CodeError(f"merge conflict (aborted, nothing changed): {(r.stdout + r.stderr).strip()[-300:]}")
        sha = _git(["rev-parse", "--short", "HEAD"], self.repo).strip()
        self.discard(ticket_id)
        return sha


def code_repo_from_cfg(cfg: dict[str, Any]) -> CodeRepo | None:
    """The configured code repository, or None if code tickets are not set up (or the folder is not a git repo)."""
    c = cfg.get("code") or {}
    repo = Path(c["repo"]) if c.get("repo") else Path.cwd()
    try:
        return CodeRepo(repo, c.get("base_branch"), c.get("worktrees_dir"))
    except (CodeError, OSError):
        return None


# ---------------------------------------------------------------------------- gates

def run_commands(commands: list[str], cwd: Path, timeout: float) -> list[dict[str, Any]]:
    """Run shell commands in `cwd`; each result is {name, ok, detail}. Bytecode and test caches are not written."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}
    results = []
    for cmd in commands:
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env)
            tail = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()[-1500:]
            results.append({"name": cmd, "ok": r.returncode == 0, "detail": f"exit {r.returncode}\n{tail}"})
        except subprocess.TimeoutExpired:
            results.append({"name": cmd, "ok": False, "detail": f"timed out after {timeout:.0f}s"})
    return results


def run_gates(repo: CodeRepo, ticket_id: str, spec: CodeSpec, code_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The automatic checks on a finished code ticket (after the orchestrator committed its worktree)."""
    changed = repo.changed_files(ticket_id)
    gates: list[dict[str, Any]] = []
    bad = [p for _, p in changed if not any(glob_match(p, a) for a in spec.allowed_paths)]
    if not changed:
        gates.append({"name": "path policy", "ok": False, "detail": "no files were changed"})
    else:
        gates.append({"name": "path policy", "ok": not bad,
                      "detail": (f"outside allowed_paths: {bad}" if bad else f"{len(changed)} file(s) changed, all allowed")})
    gcfg = code_cfg.get("gates") or {}
    if gcfg.get("secret_scan", True):
        hits = [f"file {p}" for _, p in changed
                if Path(p).name.lower() not in SECRET_ALLOWED and SECRET_FILE_RE.search(Path(p).name)]
        hits += [f"added line matches {pat.pattern[:30]}..." for line in repo.added_lines(ticket_id)
                 for pat in SECRET_PATTERNS if pat.search(line)][:5]
        gates.append({"name": "secret scan", "ok": not hits, "detail": "; ".join(hits) or "clean"})
    timeout = float(code_cfg.get("test_timeout", 600))
    wt = repo.worktree_path(ticket_id)
    gates += [{**r, "name": f"acceptance: {r['name']}"} for r in run_commands(spec.acceptance, wt, timeout)]
    gates += [{**r, "name": f"extra: {r['name']}"} for r in run_commands(list(gcfg.get("extra_commands") or []), wt, timeout)]
    return gates


def format_gate_failures(gates: list[dict[str, Any]]) -> str:
    lines = ["Your work was committed and checked automatically. These checks FAILED; fix the cause, run_tests, and reply done again:"]
    for g in gates:
        if not g["ok"]:
            lines.append(f"- {g['name']}\n{g['detail']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------- the tools

class CodeToolBox(ToolBox):
    """What a code worker may do: read and search the worktree, edit files inside allowed_paths, run the
    ticket's acceptance commands. No hardware, no installs, no arbitrary execution."""

    def __init__(self, workspace: str | Path, actor: str, *, root: Path, spec: CodeSpec, timeout: float = 600, **kw: Any) -> None:
        super().__init__(workspace, actor, **kw)
        self.root, self.spec, self.timeout = Path(root).resolve(), spec, timeout

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in CODE_TOOLS:
            return {"ok": False, "error": f"{tool} is not available for code tickets; use one of: {', '.join(CODE_TOOLS)}"}
        return super().call(tool, args)

    def describe(self) -> str:
        return "\n".join([
            "Tools (reply {\"action\": \"tool\", \"tool\": \"<name>\", \"args\": {...}}; the result comes back to you):",
            "- list_dir {path}: list a directory in the repository (relative paths; \".\" is the root).",
            "- read_file {path, start_line?, end_line?}: read a file (lines are numbered when a range is given).",
            "- search_text {pattern, path?, glob?}: regex search across the repository; returns file:line matches. Use it instead of guessing names.",
            "- write_file {path, content}: create or replace a file (new files, or small files).",
            "- edit_file {path, old, new}: replace ONE exact occurrence of `old` with `new` (prefer this for existing files).",
            "- run_tests {}: run this ticket's acceptance commands and see the output.",
            f"You may write ONLY these paths: {', '.join(self.spec.allowed_paths)}.",
            "Acceptance commands (they must all pass): " + "; ".join(self.spec.acceptance),
        ])

    # ---- paths
    def _inside(self, raw: str) -> tuple[Path, str]:
        p = (self.root / str(raw)).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"{raw} is outside the repository")
        rel = p.relative_to(self.root).as_posix() if p != self.root else "."
        if rel == ".git" or rel.startswith(".git/"):
            raise ToolError(".git is off limits")
        return p, rel

    def _writable(self, raw: str) -> tuple[Path, str]:
        p, rel = self._inside(raw)
        if p.name.lower() not in SECRET_ALLOWED and SECRET_FILE_RE.search(p.name):
            raise ToolError(f"{p.name} looks like a secrets file and cannot be written")
        if not any(glob_match(rel, a) for a in self.spec.allowed_paths):
            raise ToolError(f"{rel} is not in this ticket's allowed_paths: {self.spec.allowed_paths}")
        return p, rel

    # ---- reading
    def _t_list_dir(self, a: dict[str, Any]) -> dict[str, Any]:
        p, _ = self._inside(str(a.get("path", ".")))
        if not p.is_dir():
            raise ToolError(f"not a directory: {a.get('path')}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))[:300]:
            if child.name == ".git" or (child.is_file() and child.name.lower() not in SECRET_ALLOWED
                                        and SECRET_FILE_RE.search(child.name)):
                continue
            entries.append({"name": child.name + ("/" if child.is_dir() else ""),
                            "bytes": child.stat().st_size if child.is_file() else None})
        return {"entries": entries}

    def _t_read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p, rel = self._inside(a["path"])
        if p.name.lower() not in SECRET_ALLOWED and SECRET_FILE_RE.search(p.name):
            raise ToolError(f"{p.name} looks like a secrets file and is never readable by agents")
        if not p.is_file():
            raise ToolError(f"not a file: {rel}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if a.get("start_line") or a.get("end_line"):
            lo, hi = max(1, int(a.get("start_line") or 1)), min(len(lines), int(a.get("end_line") or len(lines)))
            body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(lo, hi + 1))
            return {"content": body[:20000], "lines": f"{lo}-{hi} of {len(lines)}"}
        text = "\n".join(lines)
        return {"content": text[:20000], "truncated": len(text) > 20000, "total_lines": len(lines)}

    def _t_search_text(self, a: dict[str, Any]) -> dict[str, Any]:
        try:
            rx = re.compile(str(a["pattern"]))
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from e
        base, _ = self._inside(str(a.get("path", ".")))
        glob = a.get("glob")
        hits: list[str] = []
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for f in files:
            if len(hits) >= 60:
                break
            if not f.is_file() or ".git" in f.relative_to(self.root).parts or f.stat().st_size > 1_000_000:
                continue
            if SECRET_FILE_RE.search(f.name) and f.name.lower() not in SECRET_ALLOWED:
                continue
            rel = f.relative_to(self.root).as_posix()
            if glob and not glob_match(rel, glob):
                continue
            try:
                head = f.read_bytes()[:1024]
                if b"\0" in head:
                    continue
                for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                        if len(hits) >= 60:
                            break
            except OSError:
                continue
        return {"matches": hits, "truncated": len(hits) >= 60}

    # ---- writing
    def _t_write_file(self, a: dict[str, Any]) -> dict[str, Any]:
        content = str(a["content"]).replace("\r\n", "\n")
        if len(content) > 200_000:
            raise ToolError("file too large (200KB max)")
        p, rel = self._writable(a["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
        return {"path": rel, "bytes": len(content.encode("utf-8"))}

    def _t_edit_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p, rel = self._writable(a["path"])
        if not p.is_file():
            raise ToolError(f"{rel} does not exist; use write_file to create it")
        old, new = str(a["old"]).replace("\r\n", "\n"), str(a["new"]).replace("\r\n", "\n")
        if not old:
            raise ToolError("`old` must not be empty")
        text = p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
        n = text.count(old)
        if n == 0:
            raise ToolError("`old` was not found in the file (it must match exactly, including whitespace)")
        if n > 1:
            raise ToolError(f"`old` matches {n} places; include more surrounding text so it matches exactly once")
        p.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        return {"path": rel, "replaced": 1}

    def _t_run_tests(self, a: dict[str, Any]) -> dict[str, Any]:
        results = run_commands(self.spec.acceptance, self.root, self.timeout)
        return {"all_passed": all(r["ok"] for r in results), "results": results}
