# Orchestrator

A small "research lab" made of AI agents, for long, open-ended technical projects.

You talk to **one** agent (your own Claude Code, Codex, or another agent). It runs a team: department managers that
persist for the whole project, and short-lived workers that each get one narrow task. The team shares a ticket
system, a tool library, durable memory and an independent verification step. A dashboard shows everything at a glance.
It works with whatever AI access you have: API keys, or just a CLI subscription, or a local model.

- **Why not one big agent?** Long projects blow up an agent's context and force it to forget. Here every worker starts
  small and focused, and the project's memory lives in files and a database, so nothing depends on any one
  conversation surviving, even a power cut.
- **What is it for?** Research-style engineering where the next step depends on the last result: surveying the
  literature, building reusable tools, running experiments (including hardware-in-the-loop rigs), and checking the
  results against the real goal. Think hardware bring-up, game-development prototypes, performance investigations.
- **What is it not for?** One-off questions or quick edits (just use your agent directly), and anything that needs a
  real security sandbox (see [Safety](#safety-and-limits)).

> **Status: early.** Built and tested with 166 automated tests against mock models, plus live runs: a full research
> task on DeepSeek models, and real calls through the Claude CLI. Not yet exercised live: Codex, the Tooling
> department's code sourcing, real hardware, game development. See [What has been verified](#what-has-been-verified).

---

## Set it up in your own project (start here)

You need: **Python 3.11+**, git, and **one** way to reach a model (an API key, *or* the `claude` / `codex` CLI logged in,
*or* a local server). Windows is where it was built and tested; macOS and Linux should work but are untested.

**1. Install the orchestrator once.** It stays in its own folder; your projects only point at it.

```bash
git clone https://github.com/huntergcook/Orca-str8.git orchestrator
cd orchestrator
pip install -e ".[anthropic]"        # drop "[anthropic]" if you will not use the Claude API
```

**2. Scaffold your project folder.** Pick the line that matches the access you have:

```bash
# You have only a CLI subscription (no API keys): every role runs on that CLI
python -m orchestrator.init ~/my-project --use-cli codex      # or: --use-cli claude

# You have API keys (default scaffold assumes DeepSeek for managers/workers/verifiers)
python -m orchestrator.init ~/my-project
```

This creates, inside `~/my-project`: `config/orchestrator.yaml`, `.env.example`, `.gitignore`, a `README.md`,
`.claude/commands/orchestrate.md` (the `/orchestrate` command for Claude Code) and `AGENTS.md` (the same instructions
for Codex and any agent that reads it). Every command in those files is bound to your project's absolute path, so the
work stays inside the folder even if your agent wanders.

**3. Give it credentials (API users only).** Copy `.env.example` to `.env` in your project and fill in the key(s) you
use. `.env` is gitignored. Never paste keys into a chat with an agent.

**4. Pick or change models** (optional; the scaffold already has a working default).

```bash
cd ~/my-project
python -m orchestrator.ctl models                  # what each role runs on, and what this machine can use
python -m orchestrator.ctl models use-cli codex    # everything on your Codex CLI's default model
python -m orchestrator.smoke                       # proves every configured model answers (a few tokens each)
```

**5. Open the dashboard** in a second terminal (it keeps running until Ctrl+C):

```bash
python -m orchestrator.dashboard --project ~/my-project --open       # http://127.0.0.1:8080
```

**6. Start your agent in the project folder and tell it what you want.** It becomes the top manager:

| Your agent | How to start |
|---|---|
| **Claude Code** | `cd ~/my-project && claude`, then `/orchestrate compare the serial libraries for microcontrollers and recommend one` |
| **Codex** | `cd ~/my-project && codex`, then *"Act as the orchestrator top manager: compare the serial libraries..."* (it reads `AGENTS.md`) |
| **Any other agent** that can run shell commands | Tell it to read `AGENTS.md` in the project and follow it |

**7. What happens next.** The top manager reads the project state, detects what models you can use, and asks you about
the goal, when to stop, and how rigorous to be (quick-and-dirty, balanced, rigorous). It then creates tickets, runs the
department managers in the background, and **tells you the moment something needs a decision**. You can answer in the
chat or in the dashboard. When the stopping criteria are met it reports back and finishes.

Everything is saved in `~/my-project/workspace/`. Close the agent, reboot, come back tomorrow: start your agent in the
folder and say the same thing; it resumes where the project left off.

---

## How it works

```
  You  <->  Top manager  (your agent: Claude Code / Codex / ...; the only one who talks to you)
              |   python -m orchestrator.ctl   (JSON commands: create tickets, run, watch, answer, ...)
              v
        Department managers   research | tooling | experiment | verification | your own
              |   tickets
              v
        Workers   (short-lived, one narrow ticket each)  ->  tools, files, background jobs
```

**Top manager.** Your agent. It owns the goal, works out the stopping criteria *with you*, asks how much rigor the goal
deserves, delegates, reviews results critically, and relays anything that needs you. It keeps no memory of its own:
every decision goes into durable notes, so a fresh session can take over.

**Department managers.** Persistent roles run by a model of your choice. Each plans its tickets, creates workers, answers
their questions, reviews their results and has the final say on its department's output. They rebuild their
understanding from notes and the ticket tree each time, so they survive restarts.

| Department | Job |
|---|---|
| **Research** | Theory, surveys, papers; decides technical direction from results |
| **Tooling** | Builds and finds reusable tools. Checks its registry first, then searches open source (GitHub, PyPI), and only then builds. The only department allowed to acquire external code |
| **Experiment** | Runs experiments: claims hardware, runs long sweeps as background jobs, watches them and fixes failures. Off by default; enable it for hardware work |
| **Verification** | Independent, strict, *advisory* checks of results against the real goal, on different models for diversity |

You can add departments (see [Customize](#customize-it)).

**Workers.** One ticket each, small context, then gone. They can read and write files inside the workspace, run Python,
search the tool registry, and file tool requests; Tooling workers can also fetch code, and Experiment workers can claim
hardware and run jobs. Workers are numbered per role (`surveyor-1`, `surveyor-2`) so you can tell them apart.

**Tickets** are the only way work moves. A ticket carries its origin trace, only the context the assignee needs,
acceptance criteria, the *rigor* level, and a return spec saying exactly what comes back. Workers can ask upward for
clarification, and tickets can be split and delegated.

**Rigor** (`quick_and_dirty`, `balanced`, `rigorous`) is set per goal by the top manager after asking you. Workers match
their effort to it, and verification judges against that bar, so a quick prototype is not gold-plated and a critical
result is not waved through.

**Verification.** When a manager says a ticket is done, independent verifiers (fresh calls, ideally different models)
check it against the acceptance criteria *and the broader goal*, with the actual files as evidence. Their report goes back
to the manager, who must weigh it heavily but keeps the final say; overriding it is logged with a reason.

**Escalations climb a chain and each level must try first.** Worker problems go to the department manager, who sees why
the worker failed (reason, a sample of its bad reply, retries used) and can retry it with guidance on a fallback model,
delegate differently, or accept the gap. Only what a manager cannot resolve reaches the top manager; only what the top
manager cannot resolve reaches you. **Safety issues and model refusals go straight to you** by policy, and nothing below
you may route around a refusal.

**Memory and recovery.** State lives in a SQLite database (fully synced) and atomically written files. After a crash or
power cut, `resume` reports what was interrupted, `recover` clears stale locks and marks dead jobs lost, and unfinished
workers restart knowing they were interrupted so they check their saved files before redoing work. Hardware claims are
**never** released automatically (its physical state is unknown): you confirm, then release.

**Models.** Each role can run on a different backend (below). Stronger models usually belong higher up. If a worker's
reply fails, it falls through your fallback chain; each manager retry moves further along it.

**Dashboard.** Needs-human (with Answer / Retry / Cancel buttons), a "being handled below you" list, the org tree with
numbered workers, hardware and jobs, token spend, tickets and an event feed. A **Resume run** button appears if unfinished
work has no run (after a restart). Read-only except those actions, which are guarded by a per-start token and
localhost-only.

---

## Choose your models: use whatever access you have

| Backend | Needs | Example |
|---|---|---|
| Claude API | `ANTHROPIC_API_KEY` | `models set --provider anthropic --model <model>` |
| DeepSeek, OpenAI | their API key | `models set --provider deepseek --model deepseek-flash` |
| Local (Ollama or any OpenAI-compatible server) | nothing | `models set --provider ollama --model llama3` |
| **Claude Code or Codex CLI** | the CLI installed and logged in, **no API key** | `models use-cli codex` |

```bash
python -m orchestrator.ctl models                                   # show current choices and what is detected
python -m orchestrator.ctl models use-cli codex                     # every manager/worker/verifier = Codex's default model
python -m orchestrator.ctl models use-cli claude --model sonnet --tiers worker
python -m orchestrator.ctl models set --tiers worker --provider deepseek --model deepseek-flash \
       --fallback deepseek:deepseek-v4-pro
```

- **"Every worker is the same model as my top manager":** `use-cli <the CLI you use>` with no `--model`. No model flag is
  passed, so each role runs whatever that CLI is normally set to. Add `--model NAME` to pin one, `--tiers worker,verifier`
  to change only some roles. Your agent can do this for you: its instructions tell it to detect what you have, ask, and
  apply it.
- **Choices are saved in `config/models.yaml`**, merged over `config/orchestrator.yaml`. Your hand-edited config is never
  rewritten; delete `models.yaml` to undo.
- **CLI backends** start a process per call: slower than an API call, they count against that CLI's usage limits, and token
  counts are rough estimates. Putting every role on one model means verification is independent but not model-diverse.
- Supported CLI presets use these invocations (tools off or read-only, no saved sessions): `claude -p ... --tools ""` and
  `codex exec --sandbox read-only ... -`. You can also define your own command-based backend in the config
  (`{type: cli, command: ["mytool", "--model", "{model}"]}`).

---

## Everyday commands

Run these from your project folder (or add `--project PATH`). Your agent runs most of them for you.

| Command | What it does |
|---|---|
| `ctl resume` | Everything needed to pick up the project: notes, journal, board, missing keys, recovery report |
| `ctl board` / `ctl ticket ID` | Tickets; what needs attention; one ticket in full with events |
| `ctl create --file t.json` | Create a ticket for a department |
| `ctl run` | Run the department managers (long; run it in the background) |
| `ctl watch` | Block until something needs attention or work finishes, then return |
| `ctl answer ID "text"` / `ctl retry ID ["guidance"]` / `ctl cancel ID` | Resolve what needs attention |
| `ctl handoff ID "why"` | The top manager could not resolve it; put it in front of the human |
| `ctl state --set` / `ctl log "..."` / `ctl human "..."` | Durable notes: plan, journal, and everything you said |
| `ctl recover` / `ctl release-resource NAME "..."` | Recover after a crash; free hardware after you checked it |
| `ctl add-resource NAME "when safe to stop"` / `ctl safe-to-stop NAME "..."` | Declare hardware for the Experiment department |
| `ctl models ...` | See and choose models (above) |
| `ctl pause` / `ctl unpause` | Halt or resume all model calls (also: create a file `workspace/PAUSE`) |
| `ctl finish "summary"` | Mark the project done |

(`ctl` = `python -m orchestrator.ctl`.) Other entry points: `python -m orchestrator.smoke` (pre-flight),
`python -m orchestrator.dashboard`, `python -m orchestrator.init`.

---

## Customize it

`config/orchestrator.yaml` in your project:

| Setting | Meaning |
|---|---|
| `models` / `providers` | Which backend runs each role (prefer `ctl models`); `fallback_chain` for retries |
| `departments` | Which departments exist: `manager_role`, `mission`, `can_acquire_tools`, `resource_leases` (hardware), `diverse_models` |
| `verification` | `enabled`, `verifier_models`, `max_rounds` |
| `budget` | `max_tokens`, `max_minutes` are enforced; `max_cost_usd` is **not yet** |
| `policies` | `capability_retry_limit`, `max_worker_retries`, `max_children_per_agent`, `default_rigor` |
| `read_roots` | Folders agents may **read** (never write), by absolute path, e.g. your existing code repo. `.env`, keys and credential files are always blocked. Output still goes to `workspace/` |
| `rules_file` | A file of mandatory project rules (legal, safety, house rules) injected into **every** manager, worker and verifier prompt; verification fails work that breaks them |

- **Hardware work:** uncomment the `experiment` department, declare each device (`ctl add-resource`), then describe the
  rig to your top manager. Agents run Python scripts and background jobs on your machine to drive it.
- **Roles are plain markdown prompts.** Put your own in `<project>/roles/` and they override the built-in ones in this
  repo's `roles/`: a manager role (`roles/gamedev_manager.md`, referenced from `departments.<name>.manager_role`) or a
  worker role (`roles/workers/<name>.md`, named by tickets' `worker_role`). A department with no file uses a generic
  template plus its `mission`.
- **Using it on an existing project:** scaffold into a *copy* first (`git clone` your repo, strip anything agent-specific you no longer want), set `read_roots` to the folder, and put your non-negotiable rules in a `rules_file`. If the project has its own top-level Python package named `orchestrator`, it can shadow this one; generated commands already use `python -P` (which stops the working directory from shadowing installed packages), and so should you when running by hand: `python -P -m orchestrator.ctl ...`.
- **More departments:** add an entry under `departments:` (for example `gamedev: {manager_role: roles/gamedev_manager.md}`).
- **Keys for tool search:** set `GITHUB_TOKEN` in `.env` to avoid GitHub's anonymous rate limit.

---

## Safety and limits

Read this before pointing it at anything that matters.

- **There is no sandbox.** Workers run Python and background jobs as *your* user on *your* machine, by design (you
  prepare a safe harness and agents explore inside it). File tools are confined to the workspace, but scripts they run are
  not. For anything risky, use a dedicated machine or VM, and keep hardware behind hardware-level safety limits.
- **Your data goes to the model providers you configure.** Tickets, context and file contents are sent to them. Keep
  sensitive work on a provider you trust, or on a local model.
- **Cost controls are partial.** Token and time budgets are enforced and `PAUSE` halts everything immediately, but dollar
  caps are not implemented. CLI backends report no real usage numbers. Verification multiplies model calls.
- **One run at a time per project** (enforced with a lock).
- **Workers cannot spawn sub-workers yet** (`max_subworker_depth` is declared but unused).
- **It is only as good as your agents.** The top manager follows written instructions; weaker agents may follow them less
  faithfully. Models sometimes return malformed replies; there are retries and fallbacks, and failures are logged and
  escalated, not hidden.
- Windows is the tested platform.

## What has been verified

- **166 automated tests**, all with mock or scripted providers plus real local subprocesses (jobs, a fake CLI backend, a
  simulated power loss, concurrency guards, the dashboard's security checks).
- **Live:** a complete research task ran end to end on DeepSeek models: the research department, workers using the file,
  Python and tool-registry tools, the verification gate, and worker escalations. The Claude CLI backend also answered a
  real call.
- **Not yet live:** the Tooling department's GitHub and PyPI sourcing, the Experiment department and real hardware, the
  Codex preset (built from OpenAI's CLI reference, never run on a real Codex), game development, macOS/Linux. A built-in chat top manager (`python -m orchestrator`, for when you have no interactive
  agent) exists but is the least-tested path.

---

## Repository layout

```
orchestrator/     the runtime (see below)
roles/            built-in role prompts (top manager, department managers, workers)
schemas/          ticket.schema.json
config/           orchestrator.example.yaml (every setting, commented)
tests/            the test suite
DESIGN.md         the design in depth
```

| Module | Purpose |
|---|---|
| `store.py` | SQLite tickets, append-only events, hardware resources, jobs; escalation routing |
| `agent.py` | Worker runner: JSON protocol, retries, fallback models, refusal policy |
| `manager.py` | Department manager: plan, run workers, answer questions, retry workers, review |
| `verification.py` | Independent advisory verification |
| `tools.py` | Tool registry, GitHub/PyPI sourcing, file and Python tools, hardware tools |
| `hardware.py` | Background jobs and the live hardware status |
| `providers.py` | Claude API, OpenAI-compatible (DeepSeek, OpenAI, Ollama), CLI backends, routing |
| `modelcfg.py` | Detect and choose models |
| `ctl.py`, `actions.py` | The control interface for the top manager; shared answer/retry/cancel/handoff |
| `dashboard.py` | The web dashboard |
| `init.py`, `smoke.py` | Project scaffold; pre-flight check |
| `memory.py`, `limits.py`, `util.py`, `config.py`, `top.py` | Notes, budgets/pause, durable I/O, config, the built-in top manager |

## Development

```bash
pip install -e ".[dev,anthropic]"
python -m pytest -q
```

## License

Public domain under the [Unlicense](LICENSE): copy, change, sell or relicense it however you like. Third-party tools the
Tooling department downloads into a project keep their own licenses, which are recorded in that project's tool registry.
