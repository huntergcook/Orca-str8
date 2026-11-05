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
  director: {{ provider: external, model: claude-code }}   # played by your Claude Code session (/orchestrate)
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
  software:                  # writes code in isolated git worktrees; branches wait for your review (needs a git repo)
    manager_role: roles/software_manager.md
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
# the brief is edited while the harness runs; tracked, it would block `code release` (needs a clean tree)
config/mission.yaml
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

0. **Fill in `config/mission.yaml`** (the mission brief): your goal, success criteria, rigor, how it should behave when
   you are away. Every agent reads it on every call, and no work starts until its required questions are answered. Or
   start your agent and let the director interview you.
1. Copy `.env.example` to `.env` and add your `DEEPSEEK_API_KEY`.
2. Pre-flight: `python -m orchestrator.smoke`
3. Dashboard (second terminal): `python -m orchestrator.dashboard` then open http://127.0.0.1:8080
4. Start your agent in this folder: Claude Code (`claude`, then `/orchestrate <what you want done>`) or Codex (`codex`, then "act as the orchestrator director: <what you want done>"; it reads AGENTS.md).
5. No API keys? Run `python -m orchestrator.ctl models use-cli codex` (or `claude`) to run every role on that CLI.

To let it run unattended until the goal is met: `python -m orchestrator.daemon start` (restarts itself if it dies), and
`python -m orchestrator.daemon install` to start it at logon. `python -m orchestrator.daemon status` shows where it is.

State lives in `workspace/` (database, memory notes, tool library, jobs, reports).
Create `workspace/PAUSE` to halt all model calls; delete it to resume.
"""


def mission_text() -> str:
    from .mission import _dump, template_data
    return _dump(template_data())


def command_text(project: str | Path | None = None) -> str:
    """The /orchestrate command. With `project`, every ctl command carries --project <abs path>, so the
    work is bound to that folder even if the session's working directory drifts."""
    src = (ORCH_HOME / ".claude" / "commands" / "orchestrate.md").read_text(encoding="utf-8")
    src = src.replace("`roles/director.md`", f"`{ORCH_HOME / 'roles' / 'director.md'}`")
    if project is not None:
        proj = str(Path(project).resolve())
        # -P: do not put the working directory on sys.path, so a project that has its own top-level
        # "orchestrator" package cannot shadow this one
        src = src.replace("python -m orchestrator.ctl", f'python -P -m orchestrator.ctl --project "{proj}"')
        src = src.replace("workspace/run.log", f'"{Path(proj).as_posix()}/workspace/run.log"')
        src = src.replace("python -m orchestrator.daemon", f'python -P -m orchestrator.daemon --project "{proj}"')
        src = src.replace("python -m orchestrator.smoke", f'python -P -m orchestrator.smoke --project "{proj}"')
        src = src.replace("python -m orchestrator.dashboard", f'python -P -m orchestrator.dashboard --project "{proj}"')
        src = src.replace("**Always run every `ctl` command from the project folder, which is this session's working directory.",
                          f"**This project is `{proj}`; every `ctl` command below already carries `--project` so all work stays inside it.")
    return src


def agents_text(project: str | Path) -> str:
    """The same director instructions as AGENTS.md, which Codex (and other agents) read automatically."""
    body = command_text(project)
    if body.startswith("---"):                                  # drop the Claude Code slash-command frontmatter
        body = body.split("---", 2)[2].lstrip()
    body = body.replace("The human's request, if any: $ARGUMENTS", "The human's request is whatever they ask you next.")
    blank = chr(10) * 2
    head = ("# Orchestrator project: director instructions" + blank +
            "This folder is run by an LLM orchestrator. When the human asks you to run, continue, check on or "
            "manage the project, you are its director and follow everything below. (Written for Codex; any "
            "agent that reads AGENTS.md works. Claude Code users get the same text as the /orchestrate command.)"
            + blank)
    return head + body


def scaffold(target: str | Path, force: bool = False) -> list[Path]:
    root = Path(target)
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"{root} is not empty (use --force to add missing files)")
    files = {
        "config/orchestrator.yaml": CONFIG.format(orch=ORCH_HOME),
        "config/mission.yaml": mission_text(),
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
    ap.add_argument("--use-cli", choices=["claude", "codex"],
                    help="run every manager/worker/verifier on this CLI (no API key needed) instead of DeepSeek")
    a = ap.parse_args()
    try:
        written = scaffold(a.target, a.force)
    except FileExistsError as e:
        print(f"error: {e}")
        return 1
    for p in written:
        print(f"created {p}")
    if a.use_cli:
        from .modelcfg import use_cli
        use_cli(Path(a.target) / "config" / "orchestrator.yaml", a.use_cli)
        print(f"models: every manager, worker and verifier now runs on the {a.use_cli} CLI (config/models.yaml)")
    if not (Path(a.target) / ".git").exists():
        print("\nnote: this folder is not a git repository. Code tickets (the Software department) need one: "
              "run `git init` and make a first commit in it, or set code.repo in config/orchestrator.yaml.")
    if a.use_cli:
        print("\nNext: fill in config/mission.yaml (`python -m orchestrator.ctl intake`), run `python -m orchestrator.smoke` from "
              "that folder, then `python -m orchestrator.daemon --project . start`. No API key is needed for the CLI.")
    else:
        print(f"\nNext: add DEEPSEEK_API_KEY to {Path(a.target) / '.env'} (copy .env.example), then run "
              "`python -m orchestrator.smoke` from that folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
