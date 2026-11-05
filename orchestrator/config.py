"""Config loader with defaults. See config/orchestrator.example.yaml."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "workspace": "./workspace",
    "mission_file": "config/mission.yaml",   # the mission brief (questionnaire the human fills in); injected into every prompt
    "notify": {"ntfy": None, "webhook": None, "command": None},   # where the daemon reaches the human (also notifications.log)
    "rules_file": None,          # path (relative to the project) of rules every agent must obey; injected into all prompts
    "code": {                   # the code-writing pipeline (code tickets)
        "repo": None,           # git repo code tickets change (default: the project folder, if it is a git repo)
        "base_branch": None,    # default: the branch checked out now
        "worktrees_dir": None,  # default: <repo>-worktrees next to the repo
        "test_timeout": 600,    # seconds per acceptance command
        "gates": {"secret_scan": True, "extra_commands": []},
        "review": "manager",    # manager: the Software manager reviews and integrates branches; director: you do (ctl code)
        "integration_branch": None,   # default: orchestrator/integration (managers merge here; ctl code release -> base branch)
        "integration_checks": [],     # commands run on the merged result; a merge that breaks them is undone
    },
    "read_roots": [],            # project folders workers may read (never write); secrets are always blocked
    "providers": {
        "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
        "deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "base_url": "https://api.deepseek.com",
                     "max_output_tokens": 32000},
        "openai": {"type": "openai_compat", "api_key_env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1"},
        "ollama": {"type": "openai_compat", "base_url": "http://localhost:11434/v1", "require_key": False},
        "claude-cli": {"type": "cli", "preset": "claude"},     # Claude Code as a backend: no API key needed
        "codex-cli": {"type": "cli", "preset": "codex"},       # Codex CLI as a backend: no API key needed
    },
    "models": {
        "director": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "manager": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "worker": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "fallback_chain": [],
    },
    "budget": {"max_cost_usd": None, "max_tokens": None, "max_minutes": None,
               "stop_below_balance_usd": None},     # halt when a provider's REAL balance (e.g. DeepSeek) drops below
    "policies": {
        "capability_retry_limit": 3,
        "on_safety_refusal": "retry_once_then_escalate_to_human",
        "max_subworker_depth": 2,
        "max_children_per_agent": 8,
        "max_parallel_tickets": 8,     # top-level tickets (departments) worked at the same time
        "max_parallel_workers": 4,     # workers per department running at the same time
        "max_concurrent_calls": 8,     # model calls in flight at once, across everything (provider rate limits)
        "max_tool_steps": 40,          # tool calls per worker ticket (with a countdown before the end)
        "max_code_tool_steps": 80,     # code workers read and edit more: their own, larger allowance
        "worker_context_chars": 200000,  # a worker's conversation size before old tool results are shortened
        "max_manager_tool_steps": 25,  # tool calls per manager decision
        "max_manager_rounds": 20,      # decisions per manager ticket before it is escalated as stuck
        "approve_cross_department_work": False,   # true: request_work tickets wait for the director (tight budgets)
        "max_worker_retries": 2,       # manager retries per worker before it must delegate differently or give up
        "default_rigor": "balanced",
        "verification_is_advisory": True,
        # persistence (daemon): how hard the harness tries before it gives up or bothers the human
        "serial_minutes": 20,          # warn (once per chain) when the work has been a one-at-a-time chain this long; 0 = off
        "drift_review_minutes": 180,   # an independent reviewer checks the plan against the mission brief this often; 0 = off
        "min_free_memory_mb": 1500,    # below this free RAM the harness halves (or drops to 1) parallel tickets
        "max_cpu_load": 1.5,           # 1-min load per core above which parallelism is halved (POSIX only)
        "outcome_routing": True,       # demote worker models with a clearly poor record in a department
        "routing_min_samples": 5,
        "max_departments": 12,         # departments the director may have in total (config + ones it creates)
        "dynamic_department_privileges": True,   # departments the director creates may write code / acquire tools (never hardware)
        "max_restarts": 6,             # times a stopped/crashed manager ticket is restarted before it is escalated
        "restart_backoff_seconds": 5,  # doubles per restart, capped at 10 minutes
        "daemon_poll_seconds": 10,     # idle loop sleep
        "stall_hours": 4,              # no progress anywhere for this long -> the director must change strategy
        "model_retry_minutes": 30,     # how long to wait before re-trying a model that hit its quota/usage limit
        "director_timeout_minutes": 20,  # a director turn that takes longer than this counts as a failure
    },
    "verification": {
        "enabled": False,        # gate manager results through independent verifiers
        "verifier_models": [],   # distinct models for diversity; empty = use the manager model chain
        "max_rounds": 2,         # verification passes per ticket before the manager's decision stands
    },
    "departments": {},
    "interfaces": {"cli": True, "dashboard": {"enabled": True, "port": 8080}},
}


def _legacy_names(data: dict[str, Any]) -> dict[str, Any]:
    """Older configs name the director's model tier `top`; accept that so existing projects keep working."""
    models = data.get("models")
    if isinstance(models, dict) and "top" in models:
        models = dict(models)
        top = models.pop("top")
        models.setdefault("director", top)
        data = {**data, "models": models}
    return data


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: str | Path) -> list[str]:
    """Minimal .env loader (KEY=VALUE lines, # comments, optional quotes). Never overrides
    variables that are already set. Returns the names it set. .env is gitignored."""
    p = Path(path)
    if not p.is_file():
        return []
    import os
    set_names = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            set_names.append(k)
    return set_names


def enter_project(project: str | Path | None = None) -> Path | None:
    """Make `project` (or $ORCHESTRATOR_PROJECT) the working directory, so config, .env, the workspace
    and every relative path resolve inside the project folder no matter where the process was started."""
    import os
    project = project or os.environ.get("ORCHESTRATOR_PROJECT")
    if not project:
        return None
    p = Path(project).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"project folder not found: {p}")
    os.chdir(p)
    return p


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    # Look for .env in the working directory and next to the config's parent (the repo root).
    load_dotenv(Path.cwd() / ".env")
    if path is not None:
        load_dotenv(Path(path).resolve().parent.parent / ".env")
    if path is None:
        return copy.deepcopy(DEFAULTS)
    data = _legacy_names((yaml.safe_load(Path(path).read_text()) or {}) if Path(path).exists() else {})
    cfg = _merge(DEFAULTS, data)
    overlay = overlay_path(path)
    if overlay.exists():                       # model choices made with `ctl models` win over the base file
        over = _legacy_names(yaml.safe_load(overlay.read_text()) or {})
        cfg = _merge(cfg, {k: v for k, v in over.items() if k in ("models", "verification", "providers")})
    return cfg


def project_rules(cfg: dict[str, Any]) -> str:
    """The project's mandatory rules (legal, safety, house rules), read fresh each call so edits apply at once.
    Empty if `rules_file` is unset or unreadable."""
    from .mission import render_charter
    charter = render_charter(cfg)              # the human's mission brief: every agent sees it, every call
    name = cfg.get("rules_file")
    rules = ""
    if name:
        p = Path(name) if Path(name).is_absolute() else Path.cwd() / name
        try:
            rules = p.read_text(encoding="utf-8").strip()[:8000]
        except OSError:
            rules = ""
    return "\n\n".join(x for x in (charter, rules) if x)


def overlay_path(config_path: str | Path) -> Path:
    return Path(config_path).resolve().parent / "models.yaml"


def is_cli_provider(cfg: dict[str, Any], name: str | None) -> bool:
    spec = cfg.get("providers", {}).get(name or "", {})
    return spec.get("type") == "cli" or "preset" in spec or "command" in spec


def qualify(cfg: dict[str, Any], provider: str | None, model: str) -> str:
    """CLI models are '<provider>:<model>' so two CLIs that both have a 'default' model stay distinct."""
    return f"{provider}:{model}" if provider and is_cli_provider(cfg, provider) else model


def model_chain(cfg: dict[str, Any], tier: str) -> list[str]:
    """Primary model for a tier, followed by capability-fallback models."""
    spec = cfg["models"][tier]
    chain = [qualify(cfg, spec.get("provider"), spec["model"])]
    for item in cfg["models"].get("fallback_chain") or []:
        m = qualify(cfg, item.get("provider"), item["model"]) if isinstance(item, dict) else item
        if m not in chain:
            chain.append(m)
    return chain
