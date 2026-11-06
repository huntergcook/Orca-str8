"""Scaffold a project folder that uses this orchestrator.

    python -m orchestrator.init C:\\dev\\my-project [--force]

Creates config, .env.example, .gitignore, a /orchestrate slash command that points at this
orchestrator by absolute path, and a README. It does not run git or write any secrets.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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

ORCA = """\
# orca.yaml - the ONE file you edit. Everything not set here has a sensible default.
# Fill in the two REQUIRED lines under `mission`, check the `models` line, and you can start.
# Edit it any time; changes apply at the next call. Every agent is shown the mission section on every call.

mission:
  goal: ""             # REQUIRED. The outcome you want (not the method). e.g. "Skyrim VR holds 90 fps in Whiterun"
  done_when: []        # REQUIRED. Checkable criteria, e.g. ["all tests pass", "frame time under 11 ms in scene X"]
  checks: []           # optional: shell commands that exit 0 when done, e.g. ["python -m pytest -q"]. Lets it finish by itself.
  never: []            # optional: off limits, must never happen, or out of scope
  quality: balanced    # quick_and_dirty | balanced | rigorous
  when_away: assume_and_continue   # or stop_and_wait. The director decides for you either way, except safety and money.
  notes: ""            # optional: anything else it should know about you or the project

models:
  cli: {cli}           # claude | codex : uses your logged-in CLI, no API key. Delete this line to use API keys (see .env.example).
  # Optional overrides, one per role. A bare name means a model of the CLI above; or write provider:model.
  director: ""         # makes the plan and the calls: the strongest model you have
  manager: ""          # splits work and reviews it: strong, mid-priced
  worker: ""           # does the bulk: fast and cheap
  verifier: ""         # checks the work: should differ from the worker
  guide: |
    director: strongest model. Few calls, expensive to get wrong (planning, trade-offs, deciding what counts as done).
    manager: strong mid-tier. Splits tickets into parallel pieces and reviews results.
    worker: fast, cheap tier for bulk reading, searching, summarising, boilerplate and well-specified edits.
      Step up to the strongest model for hard debugging, novel reasoning and the final version of important code.
    verifier: a different model (ideally a different vendor) from whatever produced the work.
    on repeated failure: retry on the next stronger model before changing the approach.

verify: true           # independent verification of results (costs more, catches more)

limits:
  parallel_workers: 8  # workers per department at once
  parallel_tickets: 12 # top-level tickets at once
  max_hours: null      # stop after this long (null = no limit)
  max_cost_usd: null   # API spend cap (null = none)
  max_tokens: null

notify:
  ntfy: ""             # e.g. https://ntfy.sh/my-secret-topic : phone notifications. Always logged to workspace/notifications.log
  webhook: ""

# departments: leave this out. Default: research, tooling, software, verification. The director adds more when the work needs them.
"""

GITIGNORE = """\
.env
workspace/
*.db
*.db-wal
*.db-shm
__pycache__/
# edited while the harness runs; tracked, it would block `code release` (needs a clean tree)
orca.yaml
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

1. **Edit `orca.yaml`**: it is the only file you need. Fill in `goal` and `done_when` (the rest has defaults), and check `models.cli`.
   Every agent is shown the mission section on every call.
2. Pre-flight: `python -m orchestrator.smoke`
3. Run it: `python -m orchestrator.daemon start` (unattended), or start your agent in this folder: Claude Code (`claude`, then
   `/orchestrate`) or Codex (it reads AGENTS.md).
4. Dashboard (optional): `python -m orchestrator.dashboard`, then open http://127.0.0.1:8080
5. Using API keys instead of a CLI: delete `cli:` in orca.yaml, copy `.env.example` to `.env`, and add the keys.

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


def scaffold(target: str | Path, force: bool = False, cli: str = "claude") -> list[Path]:
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)          # only missing files are written; nothing existing is touched
    files = {
        "orca.yaml": ORCA.format(cli=cli),
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


def detect_cli(which=shutil.which) -> str | None:
    for name in ("claude", "codex"):
        if which(name):
            return name
    return None


def _git_ready(root: Path) -> str:
    """Code tickets need a git repo with a commit. Make one if the folder has none."""
    if shutil.which("git") is None:
        return "git is not installed: code tickets (the Software department) will not work until it is"
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    if run("rev-parse", "--is-inside-work-tree").returncode == 0:
        return "git: using the existing repository"
    run("init", "-q")
    run("add", "-A")
    r = run("-c", "user.name=orca", "-c", "user.email=orca@localhost", "commit", "-q", "-m", "orca: project start")
    return "git: created a repository with a first commit" if r.returncode == 0 else f"git init done, first commit failed: {r.stderr.strip()[:120]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.init", description="Set up a project folder in one command.")
    ap.add_argument("target", nargs="?", default=".", help="project folder (default: the current folder)")
    ap.add_argument("--cli", choices=["auto", "claude", "codex", "none"], default="auto",
                    help="which logged-in CLI runs the models (auto = whichever is installed; none = use API keys)")
    ap.add_argument("--use-cli", choices=["claude", "codex"], dest="cli_old", help=argparse.SUPPRESS)
    ap.add_argument("--goal", help="fill in the goal now")
    ap.add_argument("--done-when", action="append", dest="done_when", help="a success criterion (repeat for several)")
    ap.add_argument("--check", action="append", help="a shell command that exits 0 when done (repeat for several)")
    ap.add_argument("--start", action="store_true", help="start the unattended daemon right away (needs --goal and --done-when)")
    ap.add_argument("--no-git", action="store_true", help="do not create a git repository")
    ap.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    cli = a.cli_old or a.cli
    found = detect_cli()
    if cli == "auto":
        cli = found or "none"
    root = Path(a.target).resolve()
    written = scaffold(root, a.force, cli="claude" if cli == "none" else cli)
    if cli == "none":                                   # API keys: take the cli line out of the new file
        f = root / "orca.yaml"
        if f in written:
            f.write_text("\n".join(ln for ln in f.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("cli:")) + "\n",
                         encoding="utf-8")
    for p in written:
        print(f"created {p}")
    print(f"models: {'your ' + cli + ' CLI (no API key needed)' if cli != 'none' else 'API keys: copy .env.example to .env and fill in the keys you have'}"
          + ("" if found or cli == "none" else f"   WARNING: `{cli}` was not found on PATH"))
    if not a.no_git:
        print(_git_ready(root))
    py = "python" if os.name == "nt" else "python3"
    if a.goal or a.done_when or a.check:
        from . import mission
        cfg = {"mission_file": str(root / "orca.yaml")}
        for qid, val in (("goal", a.goal), ("success_criteria", a.done_when), ("success_checks", a.check)):
            if val:
                mission.set_answer(cfg, qid, val)
        print("mission: recorded in orca.yaml")
    ready = bool(a.goal and a.done_when)
    if a.start:
        if not ready:
            print("\n--start needs --goal and --done-when; edit orca.yaml and run `orca start`.")
            return 1
        from .daemon import _spawn_detached
        _spawn_detached(root)
        print(f"\nStarted. Check on it:  cd {root} && orca status    (report: orca report)")
        return 0
    print("\nNext:")
    if not ready:
        print(f"  1. Edit {root / 'orca.yaml'}: fill in `goal` and `done_when` (everything else has defaults).")
    print(f"  {'2' if not ready else '1'}. cd {root}  and run:  orca start      (or `{py} -m orchestrator.daemon --project . start`)")
    print("     Check progress any time with `orca status`; read the result with `orca report`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
