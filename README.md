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

> **Status: early.** Built and tested with 229 automated tests against mock models, plus live runs: a full research
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
| **Software** | Changes code. Its **manager owns the repository** like a tech lead: engineers each edit their own git worktree and branch; after automatic gates the manager reviews each branch (summary, open questions, diff), fixes small things itself, and **integrates** good work into an integration branch or sends it back with feedback. Your main branch only changes when the top manager (or you) **releases** the integration branch (`ctl code release`) |
| **Verification** | Independent, strict, *advisory* checks of results against the real goal, on different models for diversity. It also reads each patch (the diff) and the project rules |

You can add departments (see [Customize](#customize-it)).

**Changing code (the Software department).** Code tickets carry a `code` spec: `allowed_paths` (narrow globs), `acceptance` (shell commands that must pass) and `context_files`. Before any worker starts, every acceptance command is run once in a throwaway checkout: one that cannot run as written (program not found, a missing script, a usage error) sends the delegation back to the manager, so nobody burns retries on a check that can never pass. The worker edits its own git worktree with a small tool set (list, read, search, `edit_file`, `write_file`, `run_tests`, `run_command`). When it says done, the orchestrator commits (build output is never committed) and runs the gates: secret scan, the acceptance commands, and a scope check that flags files changed outside `allowed_paths` for review. A failing gate goes back into the *same* conversation. The Software manager reviews each passing branch (summary, open questions, scope notes, diff), fixes small things itself, and integrates it into the integration branch, where `integration_checks` run after every merge (a merge that breaks them is undone). Your main branch changes only on `ctl code release`. The top manager can write exact worker tickets (`worker_tickets`), so ticket quality stays with the strongest model while cheap models write the code. Acceptance commands can be anything your machine can run, including C++ builds; things only a person can check (a headset, how it feels) go to you as a checklist.

**A team that works in parallel.** Every department's tickets run at the same time, and inside a department every runnable worker runs at once (capped by `max_parallel_workers`, with `max_concurrent_calls` protecting the provider's rate limits). Managers react as results arrive: they answer questions and review finished branches while slower workers are still going, so nobody waits on the slowest. Managers have their own tools and full authority to fix what a worker could not. Any agent can file work for another department (`request_work`), and workers report work they noticed in `follow_ups`; managers pass the rest up so the top manager can keep a project-wide list with no holes.

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
you may route around a refusal. Small protocol slips (a tool name sent as the action) are corrected instead of escalated,
and a model that runs out of quota (a CLI subscription limit, no API balance) is skipped for the rest of the run instead of
failing tickets. Cancelling a ticket stops the work below it at the next model call. With
`policies.approve_cross_department_work`, work one department asks of another waits as a **proposal** for the top manager.

**Memory and recovery.** State lives in a SQLite database (fully synced) and atomically written files. After a crash or
power cut, `resume` reports what was interrupted, `recover` clears stale locks and marks dead jobs lost, and unfinished
workers restart knowing they were interrupted so they check their saved files before redoing work. Hardware claims are
**never** released automatically after a crash (its physical state is unknown): you confirm, then release. A resource held
by a ticket that finished or was cancelled is released as soon as none of its jobs is still running (jobs are never killed
for this).

**Models.** Each role can run on a different backend (below). Stronger models usually belong higher up. If a worker's
reply fails, it falls through your fallback chain; each manager retry moves further along it. The top manager can also
choose models for one ticket and everything below it (`ctl set-models`), e.g. the cheap model by default and a stronger
one only for a ticket that keeps failing.

**Dashboard.** Needs-human (with Answer / Retry / Cancel buttons), a "being handled below you" list, a **Team** panel with every
manager and worker, its live state (thinking, using a tool, checking, waiting, reviewing, done) and token use, grouped by department,
token use **by department** (including prompt-cache hits), **real provider balances** next to the estimated spend, models
skipped for quota, proposals waiting for approval (Approve / Decline), the models chosen per ticket, the integration branch,
hardware and jobs, tickets and an event feed. A **Resume run** button appears if unfinished
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
| `ctl answer ID "text"` / `ctl retry ID ["guidance"]` / `ctl cancel ID` | Resolve what needs attention; cancel also stops running work and everything below it |
| `ctl handoff ID "why"` | The top manager could not resolve it; put it in front of the human |
| `ctl state --set` / `ctl log "..."` / `ctl human "..."` | Durable notes: plan, journal, and everything you said |
| `ctl recover` / `ctl release-resource NAME "..."` | Recover after a crash; free hardware after you checked it |
| `ctl add-resource NAME "when safe to stop"` / `ctl safe-to-stop NAME "..."` | Declare hardware for the Experiment department |
| `ctl code integration` / `release` / `sync` | See what the Software manager integrated, release it into your main branch, or bring main's new commits into integration |
| `ctl code list` / `show ID` / `accept ID` / `reject ID "feedback"` / `discard ID` | Review branches yourself (when `code.review: top_manager`, or to override) |
| `ctl models ...` | See and choose models (above) |
| `ctl set-models ID --manager M --worker M` | The top manager picks models for one ticket and its subtree (e.g. cheap by default, stronger for a hard or failing ticket); `default` clears |
| `ctl balance` | Real account balances from providers that report one (DeepSeek), next to the estimated spend |
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
| `budget` | `max_tokens`, `max_minutes` (per run) and (with `prices`) `max_cost_usd` are enforced. `stop_below_balance_usd` halts model calls when a provider's **real** balance drops below it (re-read every `balance_check_seconds`, default 60): the most accurate limit when your provider reports a balance |
| `policies` | `max_parallel_tickets`, `max_parallel_workers`, `max_concurrent_calls`, `max_tool_steps`, `max_manager_tool_steps`, `max_manager_rounds`, `capability_retry_limit`, `max_worker_retries`, `max_children_per_agent`, `default_rigor`, `approve_cross_department_work` (cross-department requests wait for the top manager; use it on a tight budget) |
| `code` | `review` (`manager` or `top_manager`), `integration_branch`, `integration_checks` (run after every merge; a breaking merge is undone), `test_timeout`, `gates`, `preflight` / `preflight_timeout` (acceptance commands are checked before workers start; default on, 60 s) |
| `prices` | Optional `{model: {input, output, cache_hit, off_peak}}` in $ per million tokens; enables cost estimates and `budget.max_cost_usd`. `off_peak` is a multiplier (0.5) or its own rate table, applied to calls inside `price_off_peak_utc: ["16:30", "00:30"]` |
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
- **Cost controls.** Token, time and (with a `prices` table) dollar budgets are enforced, a real-balance floor where the
  provider reports one, and `PAUSE` halts everything immediately. Estimates are only as good as your price table: check
  `ctl balance` against them.
  CLI backends report no real usage numbers. Parallel work spends faster; verification multiplies model calls.
- **One run at a time per project** (enforced with a lock).
- **Workers cannot spawn sub-workers yet** (`max_subworker_depth` is declared but unused).
- **It is only as good as your agents.** The top manager follows written instructions; weaker agents may follow them less
  faithfully. Models sometimes return malformed replies; there are retries and fallbacks, and failures are logged and
  escalated, not hidden.
- Windows is the tested platform.

## What has been verified

- **245 automated tests**, all with mock or scripted providers plus real local subprocesses (jobs, git worktrees, a fake CLI
  backend, a simulated power loss, concurrency guards, the dashboard's security checks).
- **Live, research task:** a complete research task ran end to end on DeepSeek models (research department, file, Python
  and tool-registry tools, the verification gate, worker escalations).
- **Live, a real project** (a VR mod for a 32-bit game, C++ and Python, a few dollars of DeepSeek over two sessions): all five
  departments ran in parallel. Tooling found, license-checked and acquired real open-source tools from GitHub and PyPI;
  the Software department wrote and integrated C++ and Python on worktrees, with the plugin and helper builds as
  integration checks; the Experiment department ran the actual game on a fake VR headset through a leased-PC launcher
  that always restores the user's real headset settings; verification audited the project's own notes. Most changes
  in the history since then came from what that run exposed. The Claude CLI backend (Haiku) ran there too, until it hit
  the subscription's limit, which is how quota skipping came about.
- **Not yet live:** the Codex preset (built from OpenAI's CLI reference, never run on a real Codex), real hardware other
  than a PC, macOS/Linux. A built-in chat top manager (`python -m orchestrator`, for when you have no interactive agent)
  exists but is the least-tested path.

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

[MIT](LICENSE): use, copy, change, sell or relicense it however you like; just keep the copyright and license notice. Third-party tools the
Tooling department downloads into a project keep their own licenses, which are recorded in that project's tool registry.
