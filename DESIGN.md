# Orchestrator Design

A general-purpose, hierarchical LLM orchestrator modeled on a research lab. Small agent scopes, durable memory, and a ticket system replace long-running contexts and compaction.

Status: design draft. No runtime code yet.

## 1. Goals

- **Small scopes, no compaction.** Workers get one narrow task and die. Managers persist through files, not through ever-growing contexts.
- **General.** Departments, models, budgets and rules come from config. Research-lab is the first configuration, not the only one.
- **One human interface.** Only the top manager talks to the human (CLI). A web dashboard gives read-only visibility into the whole system.
- **Autonomy with precision.** Agents explore freely inside a harness the human made safe, but each is handed a tightly scoped ticket.
- **Runs on the user's machine.** Standalone Python runtime using the user's API keys. Sandboxing/containers are a possible later feature; keep the seams for it.

## 2. Roles

```
Human <-CLI-> Top Manager
                 |-- Research Manager      -> workers -> sub-workers
                 |-- Tooling Manager       -> workers -> sub-workers
                 |-- Experiment Manager    -> workers -> sub-workers
                 |-- Verification Manager  -> workers -> sub-workers
                 |-- (more departments defined in config)
```

- **Top manager**: best available model. Owns the goal, works out the stopping criteria *with the human* via questions, delegates, and may end the run itself once the goal is sufficiently met. Can edit any manager's memory. It must be **curious about speed and performance constraints**: it asks the human how much rigor the goal deserves (quick-and-dirty, balanced, or rigorous), what time and cost pressure exists, and where correctness really matters. It records this as a **rigor profile** and stamps a `rigor` level on tickets so departments, and Verification, judge work against the right bar and don't over- or under-build.
- **Department managers**: persistent. Create workers on demand, write their own memory, hold final say on their department's output.
- **Workers / sub-workers**: ephemeral. Created from a role template plus a ticket. They finish, return what the ticket's return spec asks for, and are discarded.

### Default departments

| Department | Job |
|---|---|
| Research | Theory, surveys, papers, deciding technical direction from experimental results. Answers questions from other departments. |
| Tooling | Builds and maintains reusable tools. **Sole owner of tool acquisition** (cloning repos, installing things like Ghidra) so work isn't repeated. |
| Experiment | Requests tools for hardware automation, runs long tests/sweeps with them, oversees them and fixes failures. Files research tickets for questions beyond its scope. |
| Verification | Compares task results to the broader goal, strictly. **Advisory**: managers must weigh it heavily but keep final say. Uses diverse models across verifiers. |

### Tooling and anti-reinvention

Order for any need: **registry, then open source, then build.**

- The tool library lives in `workspace/tools/`; `registry.json` records each tool's description, usage, tags, source URL, license and who added it. Vendored repos go in `tools/vendor/<name>`.
- Every worker can `search_registry` and `request_tool` (files a ticket to Tooling with the top manager copied).
- Only departments with `can_acquire_tools` (Tooling by default) can `search_github`, `acquire_git` (https URLs only, validated names, shallow clone, license recorded, missing license flagged) and `register_tool`.
- Acquired code is never executed by the orchestrator and is treated as untrusted until inspected. There is no sandbox yet.
- Implemented in `orchestrator/tools.py`; roles in `roles/tooling_manager.md` and `roles/workers/tool_scout.md`.

## 3. Tickets

Tickets are the only way work and information move between agents. See `schemas/ticket.schema.json`.

- Flow: top manager → department manager → worker → optional sub-worker. Tickets can be split and delegated.
- Each ticket carries: a **path trace** (origin chain), the **context** the assignee needs and nothing more, the **worker role**, **acceptance criteria**, and a **return spec** (what comes back and in what shape).
- Workers can **ask upward** for clarification; this blocks the ticket in `needs_clarification` until answered.
- **Cross-department tickets** are allowed directly (e.g. Experiment → Tooling, Experiment → Research) with the top manager copied. Only Tooling does tool acquisition.
- Ticket states: `open → in_progress → (needs_clarification | blocked | escalated) → done | failed | cancelled`.

## 4. Memory

Goal: never rely on a long context.

- **Storage decision:** a single SQLite database (WAL mode) holds `tickets`, an append-only `events` table (every status change, message, question, and escalation; never updated or deleted), `resources`, and usage/spend. This gives atomic transitions, easy dashboard queries, and a full audit trail. **Manager notes, human-interaction logs and artifacts are plain markdown/files in the workspace**, readable and diffable in git. Completed tickets are also exported as JSON snapshots for the repo history.
- **Source of truth is the database + workspace files**, not model context.
- **Department managers** keep their own notes (journal + current state). On restart they rebuild from notes + their ticket log. The top manager can edit these.
- **Top manager** is held to a higher standard: it has access to the whole repo/workspace, logs every interaction with the human (decisions, stopping criteria, constraints), and structures its notes so a fresh instance can take over losslessly.
- **Workers** have no memory beyond the ticket and workspace artifacts.
- Memory layout is specified in `docs/memory.md` (TODO).

## 5. Workspace and scope

- One shared workspace per project. Per-ticket working subfolders (or branches) for artifacts.
- Each worker's context is built from: role template + ticket + only the files the ticket points at.
- Hardware access goes through tools. Experiment workers should prefer requesting an automation tool over driving hardware by hand and then oversee the long runs/sweeps those tools perform.
- **Hardware availability is owned by the Experiment manager.** A `resources` table holds one row per physical resource: `state` (free / reserved / busy), `holder_ticket`, `expected_duration`, `started_at`, `last_heartbeat`, and `safe_to_stop` (a note the manager keeps current). Workers check the flag before use. To avoid check-then-use races, they *claim* via a single atomic update that the Experiment manager's policy allows, not by reading and then acting. The manager always knows what is running and whether it is safe to stop it.

## 6. Models and fallback

- Config maps roles/depths to models; higher in the hierarchy → stronger model.
- **Capability fallback (automatic):** if a worker is stuck, the manager narrows the task and retries, possibly on a different model, looping until done or a retry limit is hit, then escalates upward.
- **Safety refusals (human-gated):** worker restates the task with its genuine context and retries once; if refused again, the ticket escalates to the human with the refusal attached. Only the human may reroute it to another model. The system never hides intent, splits a task to disguise it, or silently model-hops past a refusal.
- Provider layer is an interface; no provider is hard-wired.

## 7. Safety and escalation

- The human builds a harness safe enough for agents to explore freely.
- Anything that looks like a real safety issue is escalated up to the top manager, who relays it to the human.
- Budgets default to none; config can set token/cost/time caps globally or per department. A pause/kill control exists from day one.

## 8. Interfaces

- **CLI**: conversation with the top manager only.
- **Web dashboard**: read-only, minimal. A small Python (FastAPI) server serves one static page that reads SQLite and updates live. No frontend framework. Panels, in priority order:
  1. **Needs human**: open escalations, safety issues, refusals, pending top-manager questions.
  2. **Org tree**: managers and live workers with status and current ticket.
  3. **Ticket board**: counts and list by status, with path trace on click.
  4. **Hardware**: each resource's state, holder, and safe-to-stop note.
  5. **Spend**: tokens/cost/time vs. budget if one is set.
  6. **Event feed**: recent events.

## 9. Stack

- Python for the runtime (LLM-bound, not CPU-bound), favoring mature open-source libraries over custom code.
- C/C++ only for proven hot spots or hardware drivers, behind tool interfaces.
- Storage starts simple (files + SQLite) so the state is inspectable and diffable.

## 10. Open questions

- Decided, revisit if it hurts: SQLite + markdown storage; Experiment-manager-owned hardware flags with atomic claims; sub-worker depth cap of 2 and fan-out of 5 (config values); FastAPI + static dashboard.
- Rigor levels: exact definitions and how Verification scales strictness to them.
- Later: container/VM sandboxing, multi-provider routing details.

## 11. Suggested build order

1. SQLite ticket/event store + schema validation.
2. Agent runner: build a context from role + ticket, call a provider, write results.
3. Manager loop with notes-based resume.
4. Top manager CLI with human-interaction log.
5. Config loader (departments, models, budgets).
6. Dashboard (read-only).
7. Departments one at a time, Tooling first.
