# Orchestrator

A hierarchical LLM orchestrator modeled on a research lab: a top manager, persistent department managers, and ephemeral narrow-scope workers, connected by a ticket system. Designed to keep contexts small and avoid compaction on long, open-ended projects.

**Status:** design skeleton. No runtime yet.

- [DESIGN.md](DESIGN.md): architecture, roles, tickets, memory, safety, build order
- [schemas/ticket.schema.json](schemas/ticket.schema.json): ticket format
- [config/orchestrator.example.yaml](config/orchestrator.example.yaml): departments, models, budgets, policies
- [roles/](roles/): role prompt templates

## Intended usage
Clone this repo into (or alongside) a project, supply API keys via environment variables, copy the example config, and launch the CLI to talk to the top manager. A web dashboard shows the whole system.
