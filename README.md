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
