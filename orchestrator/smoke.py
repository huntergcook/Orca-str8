"""Pre-flight check:  python -m orchestrator.smoke [--config PATH]

Makes one tiny call (a few tokens) to every distinct model in the config so you find out about a
missing key, an inaccessible model or a bad model ID before starting a real run. Prints no secrets.
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config, model_chain
from .providers import build_provider


def configured_models(cfg: dict) -> list[str]:
    models: list[str] = []
    for tier in ("top", "manager", "worker"):
        models += model_chain(cfg, tier)
    models += list(cfg.get("verification", {}).get("verifier_models") or [])
    return list(dict.fromkeys(models))


def main() -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.smoke")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    models = configured_models(cfg)
    print(f"Checking {len(models)} model(s) from {args.config}: {', '.join(models)}")
    try:
        provider = build_provider(cfg)
    except Exception as e:  # missing credentials surface here or on first call
        print(f"FAIL  could not build the provider: {type(e).__name__}: {e}")
        return 1
    bad = 0
    for m in models:
        try:
            c = provider.complete(model=m, system="Reply with exactly one word.",
                                  messages=[{"role": "user", "content": "Say: ok"}], max_tokens=1024)
            status = "refused" if c.refused else (c.text.strip()[:30] or "(empty text)")
            print(f"ok    {m}: {status}  [{c.input_tokens} in / {c.output_tokens} out]")
        except Exception as e:
            bad += 1
            print(f"FAIL  {m}: {type(e).__name__}: {str(e)[:200]}")
    print("All models reachable." if not bad else f"{bad} model(s) failed; fix the config or your access.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
