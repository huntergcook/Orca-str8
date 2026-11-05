"""CLI: talk to the director.   python -m orchestrator [--config PATH] [--db PATH]"""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .limits import GuardedProvider, Halted
from .providers import build_provider
from .store import Store
from .director import Director


def main() -> None:
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("--config", default="config/orchestrator.yaml")
    ap.add_argument("--db", default=None, help="SQLite path (default: <workspace>/orchestrator.db)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    workspace = Path(cfg["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    store = Store(args.db or workspace / "orchestrator.db")
    provider = GuardedProvider(build_provider(cfg), store, cfg, workspace)
    top = Director(store, provider, cfg, workspace)

    print("Orchestrator director. Ctrl+C or /quit to exit; /board shows tickets.")
    print(f"Create {workspace / 'PAUSE'} to halt all model calls.")
    try:
        while not top.finished:
            try:
                text = input("\nyou> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/board":
                print(top.board())
                continue
            print(f"\ntop> {top.turn(text)}")
        if top.finished:
            print("\n[Director has ended the run.]")
    except KeyboardInterrupt:
        print()
    finally:
        store.close()


if __name__ == "__main__":
    main()
