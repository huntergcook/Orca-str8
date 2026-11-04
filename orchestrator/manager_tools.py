"""A manager's own hands. Managers have full authority to do what their goal needs, including fixing what a worker
could not: they read the project and workspace, write files in the workspace, run Python, file work for other
departments and, when they own a code repository, edit and test any of their workers' branches or the integration
branch directly (like a tech lead fixing a pull request before merging it)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .codework import GIT_ID, CodeSpec, CodeToolBox, _git, run_commands
from .tools import ToolBox, ToolError

BRANCH_TOOLS = ("branch_list", "branch_read", "branch_search", "branch_edit", "branch_write", "branch_test", "branch_run")


class ManagerToolBox(ToolBox):
    def __init__(self, workspace: str | Path, actor: str, *, code_repo: Any = None, code_cfg: dict[str, Any] | None = None,
                 my_tickets: Callable[[], set[str]] | None = None, **kw: Any) -> None:
        super().__init__(workspace, actor, **kw)
        self.code_repo, self.code_cfg, self.my_tickets = code_repo, code_cfg or {}, my_tickets

    def describe(self) -> str:
        text = super().describe().replace("Tools (reply", "Your own tools (reply", 1)
        if self.code_repo is not None:
            text += "\n".join([
                "",
                "Repository tools (you own the repository: fix a worker's branch yourself when that is quicker than sending it back):",
                "- branch_list {ticket, path?} / branch_read {ticket, path, start_line?, end_line?} / branch_search {ticket, pattern, glob?}",
                "- branch_edit {ticket, path, old, new} / branch_write {ticket, path, content}",
                "- branch_test {ticket, commands?}: run that ticket's acceptance commands (or the commands you give) in its worktree",
                "- branch_run {ticket, command}: run any shell command in that worktree",
                "  `ticket` is one of your worker ticket ids, or \"integration\" for the integration branch. Your edits to a worker",
                "  branch are committed and re-checked when you integrate it; edits to integration are committed at once.",
            ])
        return text

    def _branch(self, a: dict[str, Any]) -> tuple[str, Path, CodeToolBox]:
        if self.code_repo is None:
            raise ToolError("no code repository is configured for this department")
        tid = str(a.get("ticket", "")).strip()
        timeout = float(self.code_cfg.get("test_timeout", 600))
        if tid == "integration":
            root = self.code_repo.ensure_integration()
            spec = CodeSpec(["**"], list(self.code_cfg.get("integration_checks") or []))
        else:
            if self.my_tickets is not None and tid not in self.my_tickets():
                raise ToolError(f"{tid!r} is not one of your worker tickets (or use \"integration\")")
            t = self.store.get_ticket(tid) if self.store else {}
            if t.get("code") is None:
                raise ToolError(f"{tid} is not a code ticket")
            root = self.code_repo.ensure_worktree(tid)
            spec = CodeSpec(["**"], list(t["code"].get("acceptance") or []))
        return tid, root, CodeToolBox(self.workspace, self.actor, root=root, spec=spec, timeout=timeout)

    def _commit_if_integration(self, tid: str, root: Path, what: str) -> None:
        if tid != "integration":
            return
        _git(["add", "-A"], root)
        if _git(["status", "--porcelain"], root).strip():
            _git([*GIT_ID, "commit", "-q", "-m", f"{self.actor}: {what}"], root)

    def _t_branch_list(self, a: dict[str, Any]) -> dict[str, Any]:
        _, _, box = self._branch(a)
        return box._t_list_dir({"path": a.get("path", ".")})

    def _t_branch_read(self, a: dict[str, Any]) -> dict[str, Any]:
        _, _, box = self._branch(a)
        return box._t_read_file(a)

    def _t_branch_search(self, a: dict[str, Any]) -> dict[str, Any]:
        _, _, box = self._branch(a)
        return box._t_search_text(a)

    def _t_branch_edit(self, a: dict[str, Any]) -> dict[str, Any]:
        tid, root, box = self._branch(a)
        out = box._t_edit_file(a)
        self._commit_if_integration(tid, root, f"edit {a.get('path')}")
        return out

    def _t_branch_write(self, a: dict[str, Any]) -> dict[str, Any]:
        tid, root, box = self._branch(a)
        out = box._t_write_file(a)
        self._commit_if_integration(tid, root, f"write {a.get('path')}")
        return out

    def _t_branch_test(self, a: dict[str, Any]) -> dict[str, Any]:
        _, root, box = self._branch(a)
        commands = [str(c) for c in (a.get("commands") or box.spec.acceptance)]
        if not commands:
            raise ToolError("no commands: give `commands`, or set acceptance on the ticket / integration_checks in the config")
        results = run_commands(commands, root, box.timeout)
        return {"all_passed": all(r["ok"] for r in results), "results": results}

    def _t_branch_run(self, a: dict[str, Any]) -> dict[str, Any]:
        _, root, box = self._branch(a)
        return run_commands([str(a["command"])], root, box.timeout)[0]
