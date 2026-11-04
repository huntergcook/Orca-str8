"""Scaffold a project folder that uses this orchestrator.

    python -m orchestrator.init C:\\dev\\my-project [--force]

Creates config, .env.example, .gitignore, a /orchestrate slash command that points at this
orchestrator by absolute path, and a README. It does not run git or write any secrets.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ORCH_HOME = Path(__file__).resolve().parents[1]

CONFIG = """\
# Project config for the orchestrator at {orch}
# Keys come from environment variables or a gitignored .env in this folder.
workspace: ./workspace

providers:
  deepseek:  {{ api_key_env: DEEPSEEK_API_KEY, base_url: "https://api.deepseek.com" }}
  anthropic: {{ api_key_env: ANTHROPIC_API_KEY }}

models:
  top:     {{ provider: external, model: claude-code }}   # played by your Claude Code session (/orchestrate)
  manager: {{ provider: deepseek, model: deepseek-v4-pro }}
  worker:  {{ provider: deepseek, model: deepseek-flash }}
  fallback_chain: [{{ provider: deepseek, model: deepseek-v4-pro }}]   # tried when a worker's reply fails

budget:                      # safety net; remove or raise once you trust a run
  max_tokens: 400000
  max_minutes: 45

verification:
  enabled: true
  verifier_models: [deepseek-v4-pro, deepseek-flash]
  max_rounds: 2

departments:
  research:
    manager_role: roles/research_manager.md
  tooling:
    manager_role: roles/tooling_manager.md
    can_acquire_tools: true
  verification:
    manager_role: roles/verification_manager.md
    diverse_models: true
  # experiment:                # enable for hardware work
  #   manager_role: roles/experiment_manager.md
  #   resource_leases: true

interfaces:
  cli: true
  dashboard: {{ enabled: true, port: 8080 }}
"""

GITIGNORE = """\
.env
workspace/
*.db
*.db-wal
*.db-shm
__pycache__/
"""

ENV_EXAMPLE = """\
# Copy to .env (gitignored) and fill in. Never commit real keys.
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
"""

README = """\
# {name}

Project run by the orchestrator at `{orch}`.

1. Copy `.env.example` to `.env` and add your `DEEPSEEK_API_KEY`.
2. Pre-flight: `python -m orchestrator.smoke`
3. Dashboard (second terminal): `python -m orchestrator.dashboard` then open http://127.0.0.1:8080
4. Start Claude Code in this folder (`claude`) and type `/orchestrate <what you want done>`.

State lives in `workspace/` (database, memory notes, tool library, jobs, reports).
Create `workspace/PAUSE` to halt all model calls; delete it to resume.
"""


def command_text(project: str | Path | None = None) -> str:
    """The /orchestrate command. With `project`, every ctl command carries --project <abs path>, so the
    work is bound to that folder even if the session's working directory drifts."""
    src = (ORCH_HOME / ".claude" / "commands" / "orchestrate.md").read_text(encoding="utf-8")
    src = src.replace("`roles/top_manager.md`", f"`{ORCH_HOME / 'roles' / 'top_manager.md'}`")
    if project is not None:
        proj = str(Path(project).resolve())
        src = src.replace("python -m orchestrator.ctl", f'python -m orchestrator.ctl --project "{proj}"')
        src = src.replace("workspace/run.log", f'"{Path(proj).as_posix()}/workspace/run.log"')
        src = src.replace("python -m orchestrator.smoke", f'python -m orchestrator.smoke --project "{proj}"')
        src = src.replace("python -m orchestrator.dashboard", f'python -m orchestrator.dashboard --project "{proj}"')
        src = src.replace("**Always run every `ctl` command from the project folder, which is this session's working directory.",
                          f"**This project is `{proj}`; every `ctl` command below already carries `--project` so all work stays inside it.")
    return src


def agents_text(project: str | Path) -> str:
    """The same top-manager instructions as AGENTS.md, which Codex (and other agents) read automatically."""
    body = command_text(project)
    if body.startswith("---"):                                  # drop the Claude Code slash-command frontmatter
        body = body.split("---", 2)[2].lstrip()
    body = body.replace("The human's request, if any: $ARGUMENTS", "The human's request is whatever they ask you next.")
    blank = chr(10) * 2
    head = ("# Orchestrator project: top manager instructions" + blank +
            "This folder is run by an LLM orchestrator. When the human asks you to run, continue, check on or "
            "manage the project, you are its top manager and follow everything below. (Written for Codex; any "
            "agent that reads AGENTS.md works. Claude Code users get the same text as the /orchestrate command.)"
            + blank)
    return head + body


def scaffold(target: str | Path, force: bool = False) -> list[Path]:
    root = Path(target)
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"{root} is not empty (use --force to add missing files)")
    files = {
        "config/orchestrator.yaml": CONFIG.format(orch=ORCH_HOME),
        ".env.example": ENV_EXAMPLE,
        ".gitignore": GITIGNORE,
        "README.md": README.format(name=root.name, orch=ORCH_HOME),
        ".claude/commands/orchestrate.md": command_text(root),
        "AGENTS.md": agents_text(root),
    }
    written = []
    for rel, text in files.items():
        p = root / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.init")
    ap.add_argument("target")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        written = scaffold(a.target, a.force)
    except FileExistsError as e:
        print(f"error: {e}")
        return 1
    for p in written:
        print(f"created {p}")
    print(f"\nNext: add DEEPSEEK_API_KEY to {Path(a.target) / '.env'} (copy .env.example), then run "
          "`python -m orchestrator.smoke` from that folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
