"""Config loader with defaults. See config/orchestrator.example.yaml."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "workspace": "./workspace",
    "providers": {},
    "models": {
        "top": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "manager": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "worker": {"provider": "anthropic", "model": "claude-opus-5-5"},
        "fallback_chain": [],
    },
    "budget": {"max_cost_usd": None, "max_tokens": None, "max_minutes": None},
    "policies": {
        "capability_retry_limit": 3,
        "on_safety_refusal": "retry_once_then_escalate_to_human",
        "max_subworker_depth": 2,
        "max_children_per_agent": 5,
        "default_rigor": "balanced",
        "verification_is_advisory": True,
    },
    "verification": {
        "enabled": False,        # gate manager results through independent verifiers
        "verifier_models": [],   # distinct models for diversity; empty = use the manager model chain
        "max_rounds": 2,         # verification passes per ticket before the manager's decision stands
    },
    "departments": {},
    "interfaces": {"cli": True, "dashboard": {"enabled": True, "port": 8080}},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None or not Path(path).exists():
        return copy.deepcopy(DEFAULTS)
    data = yaml.safe_load(Path(path).read_text()) or {}
    return _merge(DEFAULTS, data)


def model_chain(cfg: dict[str, Any], tier: str) -> list[str]:
    """Primary model for a tier, followed by capability-fallback models."""
    chain = [cfg["models"][tier]["model"]]
    for item in cfg["models"].get("fallback_chain") or []:
        m = item["model"] if isinstance(item, dict) else item
        if m not in chain:
            chain.append(m)
    return chain
