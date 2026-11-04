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
python -m orchestrator            # CLI: talk to the top manager
python -m orchestrator.dashboard  # read-only dashboard on http://127.0.0.1:8080
pytest
```

## Layout
- `orchestrator/store.py`: SQLite tickets, append-only events, hardware resources
- `orchestrator/agent.py`: worker runner (capability retries, refusal policy)
- `orchestrator/manager.py`: department manager loop (plan, run workers, answer questions, review)
- `orchestrator/top.py`: top manager (rebuilds context from files each turn)
- `orchestrator/memory.py`, `config.py`, `providers.py`, `dashboard.py`
