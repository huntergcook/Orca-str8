"""Choose which models run each role, whatever access you have: API keys, a CLI subscription
(Claude Code, Codex), or a local server. Used by `python -m orchestrator.ctl models ...`.

Choices are written to config/models.yaml, which load_config() merges over config/orchestrator.yaml, so
your hand-edited config is never rewritten.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import load_config, overlay_path, qualify
from .providers import CLI_PRESETS, provider_kind

TIERS = ("manager", "worker", "verifier", "director")
DEFAULT_TIERS = ("manager", "worker", "verifier")


def _probe(url: str) -> bool:
    try:
        import httpx
        return httpx.get(url, timeout=0.7).status_code < 500
    except Exception:
        return False


def detect(env: dict[str, str] | None = None, which: Callable[[str], str | None] = shutil.which,
           probe: Callable[[str], bool] = _probe) -> dict[str, Any]:
    """What this machine can actually use right now, so the choice is made from facts."""
    env = os.environ if env is None else env
    api = {"anthropic": bool(env.get("ANTHROPIC_API_KEY")), "deepseek": bool(env.get("DEEPSEEK_API_KEY")),
           "openai": bool(env.get("OPENAI_API_KEY"))}
    cli = {name: which(p["exe"]) for name, p in CLI_PRESETS.items()}
    local = {"ollama": probe("http://localhost:11434/api/tags")}
    options = []
    for name, path in cli.items():
        if path:
            options.append(f"use-cli {name}: every role runs on your {name} CLI (no API key; model = the CLI's own default)")
    for name, ok in api.items():
        if ok:
            options.append(f"set --provider {name} --model <model>: API access to {name} is configured")
    if local["ollama"]:
        options.append("set --provider ollama --model <model>: a local Ollama server is running")
    if not options:
        options.append("nothing detected: install and log in to the claude or codex CLI, or set an API key in .env")
    return {"api_keys": api, "cli": cli, "local": local, "options": options,
            "note": "anthropic can also work through an `ant auth login` profile even when ANTHROPIC_API_KEY is unset"}


def current(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tier in ("director", "manager", "worker"):
        s = cfg["models"][tier]
        out[tier] = {"provider": s.get("provider"), "model": s["model"], "id": qualify(cfg, s.get("provider"), s["model"])}
    out["fallback_chain"] = cfg["models"].get("fallback_chain") or []
    out["verifier_models"] = cfg["verification"].get("verifier_models") or []
    out["verification_enabled"] = bool(cfg["verification"].get("enabled"))
    return out


def _read_overlay(config_path: str | Path) -> dict[str, Any]:
    p = overlay_path(config_path)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def _write_overlay(config_path: str | Path, data: dict[str, Any]) -> Path:
    from .util import atomic_write
    p = overlay_path(config_path)
    header = ("# Written by `ctl models`. Merged over orchestrator.yaml; delete this file to undo.\n")
    atomic_write(p, header + yaml.safe_dump(data, sort_keys=False))
    return p


def set_models(config_path: str | Path, tiers: list[str], provider: str, model: str,
               fallback: list[str] | None = None, clear_fallback: bool = False) -> dict[str, Any]:
    """Point the given tiers at provider/model. 'verifier' sets verification.verifier_models."""
    tiers = ["director" if t == "top" else t for t in tiers]      # `top` is the director's old tier name
    bad = [t for t in tiers if t not in TIERS]
    if bad:
        raise ValueError(f"unknown tier(s) {bad}; choose from {list(TIERS)}")
    cfg = load_config(config_path)
    if provider not in cfg["providers"]:
        raise ValueError(f"unknown provider {provider!r}; known: {sorted(cfg['providers'])}")
    over = _read_overlay(config_path)
    for tier in tiers:
        if tier == "verifier":
            over.setdefault("verification", {})["verifier_models"] = [qualify(cfg, provider, model)]
        else:
            over.setdefault("models", {})[tier] = {"provider": provider, "model": model}
    if clear_fallback:
        over.setdefault("models", {})["fallback_chain"] = []
    if fallback:
        chain = []
        for item in fallback:
            prov, _, m = item.partition(":")
            if not m or prov not in cfg["providers"]:
                raise ValueError(f"--fallback must look like provider:model with a known provider, got {item!r}")
            chain.append({"provider": prov, "model": m})
        over.setdefault("models", {})["fallback_chain"] = chain
    path = _write_overlay(config_path, over)
    after = load_config(config_path)
    return {"written": str(path), "now": current(after), "kind": provider_kind(provider, cfg["providers"][provider])}


def use_cli(config_path: str | Path, cli: str, model: str = "default",
            tiers: list[str] | None = None) -> dict[str, Any]:
    """Run the given tiers (default: manager, worker, verifier) on one CLI with one model: for example
    `use-cli codex` makes every worker the same Codex model you use as the director. model='default'
    passes no model flag, so the CLI uses whatever it is normally set to. Fallback models from other
    providers are cleared, since they may not be accessible without API keys."""
    if cli not in CLI_PRESETS:
        raise ValueError(f"unknown CLI {cli!r}; supported: {sorted(CLI_PRESETS)}")
    out = set_models(config_path, list(tiers or DEFAULT_TIERS), f"{cli}-cli", model, clear_fallback=True)
    out["note"] = ("All selected roles use the same model, so verification is independent calls but not model-diverse. "
                   "Each call spawns the CLI, so runs are slower than API calls and count against its usage limits.")
    return out
