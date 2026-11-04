# Orchestrator

A hierarchical LLM orchestrator modeled on a research lab: a top manager, persistent department managers, and ephemeral narrow-scope workers, connected by a ticket system. Designed to keep contexts small and avoid compaction on long, open-ended projects.

**Status:** early runtime. Ticket store, worker runner, department managers, top manager CLI and a read-only dashboard exist and are tested against a mock provider. Not yet exercised against a live model or real hardware.

- [DESIGN.md](DESIGN.md): architecture, roles, tickets, memory, safety, build order
- [schemas/ticket.schema.json](schemas/ticket.schema.json): ticket format
- [config/orchestrator.example.yaml](config/orchestrator.example.yaml): departments, models, budgets, policies
- [roles/](roles/): role prompt templates

## Intended usage
Clone this repo into (or alongside) a project, supply API keys via environment variables, copy the example config, and launch the CLI to talk to the top manager. A web dashboard shows the whole system.

## Quick start
```
pip install -e ".[dev,anthropic]"
cp config/orchestrator.example.yaml config/orchestrator.yaml   # set models, departments
export ANTHROPIC_API_KEY=...                                    # or `ant auth login`
export DEEPSEEK_API_KEY=...                                     # only if a tier uses DeepSeek
python -m orchestrator.smoke      # pre-flight: one tiny call per configured model
python -m orchestrator            # CLI: talk to the top manager
python -m orchestrator.dashboard  # read-only dashboard on http://127.0.0.1:8080
pytest
```

## Claude Code as the top manager (uses your subscription)
Set the top tier to `{ provider: external, model: claude-code }` and run `claude` in this repo. Then type
`/orchestrate <what you want done>`. Your Claude Code session plays the top manager and drives the lab through
`python -m orchestrator.ctl` (resume, create, run, board, answer, retry, state, log, finish, pause). Department
managers, workers and verifiers run on the API providers you configure (e.g. DeepSeek), so no Claude API key is
needed. All state is in `workspace/`, so a fresh session resumes with `ctl resume`.

## Models: use whatever access you have
The lab does not care whether you have API keys, only a CLI subscription, or a local model. Each role (manager, worker, verifier) can run on:

| Backend | Needs | Example |
|---|---|---|
| Claude API | `ANTHROPIC_API_KEY` | `models set --provider anthropic --model <model>` |
| DeepSeek, OpenAI | their API key | `models set --provider deepseek --model deepseek-flash` |
| Local (Ollama, any OpenAI-compatible server) | nothing | `models set --provider ollama --model llama3` |
| **Claude Code or Codex CLI** | the CLI installed and logged in; **no API key** | `models use-cli codex` |

```
python -m orchestrator.ctl models                      # what each role runs on now, and what this machine can use
python -m orchestrator.ctl models use-cli codex        # every manager/worker/verifier = your Codex CLI's default model
python -m orchestrator.ctl models use-cli claude --model sonnet --tiers worker
python -m orchestrator.ctl models set --tiers worker --provider deepseek --model deepseek-flash --fallback deepseek:deepseek-v4-pro
python -m orchestrator.smoke                           # one tiny call per configured model
```
`use-cli` with no `--model` passes no model flag, so every role runs the same model you normally use in that CLI. Your top manager (Claude Code or Codex) can run these for you: its instructions tell it to detect what you have, ask, and apply. Choices go in `config/models.yaml` (merged over `orchestrator.yaml`; delete it to undo). CLI backends start a process per call, so they are slower and count against that CLI's usage limits. A project scaffolded with `python -m orchestrator.init` gets both a `/orchestrate` command (Claude Code) and an `AGENTS.md` (Codex).

## Escalations
Problems climb worker -> department manager -> top manager -> you, and each level must try to fix it first. Managers see why a worker failed, can retry it with guidance on a fallback model, or delegate differently; only what they cannot resolve reaches the top manager, and only what it cannot resolve reaches you. Safety issues and model refusals go straight to you by policy. The dashboard's Needs-human panel has Answer / Retry / Cancel; "Being handled below you" shows the rest.

## Power loss and restarts
State lives in SQLite (fully synced) and atomically written files. After a crash or power cut, reopen your top manager: `resume` reports what was interrupted, `recover` clears stale locks and marks dead jobs lost, and unfinished workers restart knowing they were interrupted. Hardware claims are never released automatically; you confirm the device state, then `release-resource`. The dashboard shows a Resume run button.

## Layout
- `orchestrator/ctl.py`: control interface for an external top manager
- `.claude/commands/orchestrate.md`: the `/orchestrate` slash command
- `orchestrator/store.py`: SQLite tickets, append-only events, hardware resources
- `orchestrator/agent.py`: worker runner (capability retries, refusal policy)
- `orchestrator/manager.py`: department manager loop (plan, run workers, answer questions, review)
- `orchestrator/top.py`: top manager (rebuilds context from files each turn)
- `orchestrator/tools.py`: tool registry, Python/GitHub sourcing, file and job tools for workers
- `orchestrator/hardware.py`: background jobs and hardware status
- `orchestrator/memory.py`, `config.py`, `providers.py`, `dashboard.py`

## License
Public domain under the [Unlicense](LICENSE): copy, change, sell or relicense it however you like. Third-party tools that the Tooling department vendors into `workspace/tools/vendor/` keep their own licenses, which are recorded in the tool registry.
