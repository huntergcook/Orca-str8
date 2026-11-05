"""Score the harness on canned goals so changes to it can be judged, not guessed.

    python -m orchestrator.bench run bench/goals.yaml [--config config/orchestrator.yaml] [--only NAME] [--max-minutes 45]
    python -m orchestrator.bench compare [bench/results.jsonl]

Each goal runs in a fresh temp project (files from the goal spec, a filled-in mission brief, the machine checks from the spec)
under the real Daemon. Results append to bench/results.jsonl. A run that does not finish within `--max-minutes` is stopped and
scored as unfinished."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from . import mission
from .config import load_config
from .daemon import Daemon
from .limits import cost_estimate
from .store import Store


def _git_init(project: Path) -> None:
    (project / ".gitignore").write_text("workspace/\nmission.yaml\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-q", "-m", "bench start"]):
        subprocess.run(["git", *args], cwd=str(project), check=True, capture_output=True)


def run_goal(spec: dict[str, Any], base: str | Path, config: str | Path | None = None, provider: Any = None,
             max_minutes: float = 45.0, cfg_patch: dict[str, Any] | None = None) -> dict[str, Any]:
    project = Path(base) / spec["name"]
    project.mkdir(parents=True, exist_ok=True)
    for rel, text in (spec.get("files") or {}).items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git_init(project)                                  # code tickets need a repo, and it must be THIS project's
    cfg = load_config(config)
    cfg["workspace"] = str(project / "workspace")
    cfg.setdefault("code", {})["repo"] = str(project)    # never fall back to whatever repo the process was started in
    cfg["mission_file"] = str(project / "mission.yaml")
    cfg["policies"]["daemon_poll_seconds"] = min(float(cfg["policies"].get("daemon_poll_seconds", 5)), 2)
    for k, v in (cfg_patch or {}).items():
        cfg["policies"][k] = v
    mission.scaffold(cfg["mission_file"])
    answers = {"goal": spec["goal"], "success_criteria": spec.get("success_criteria") or ["the goal is met"],
               "success_checks": spec.get("success_checks") or [], "rigor": spec.get("rigor", "balanced"),
               "autonomy": "assume_and_continue", "model_access": "whatever the config provides"}
    for k, v in answers.items():
        mission.set_answer(cfg, k, v)
    d = Daemon(project, cfg=cfg, provider=provider, notifier=lambda *a, **k: True)
    t0 = time.time()
    box: dict[str, Any] = {}
    th = threading.Thread(target=lambda: box.update(phase=d.run()), daemon=True)
    th.start()
    th.join(max_minutes * 60)
    if th.is_alive():
        d.stop_file.write_text("bench timeout", encoding="utf-8")
        th.join(120)
    store = Store(d.workspace / "orchestrator.db")
    try:
        tot = store.usage_totals()
        cost = cost_estimate(store, cfg)
    finally:
        store.close()
    return {"goal": spec["name"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "finished": (d.workspace / "FINISHED").exists(),
            "minutes": round((time.time() - t0) / 60, 2), "calls": tot["calls"],
            "tokens": tot["input_tokens"] + tot["output_tokens"], "cost_usd": cost, "phase": box.get("phase")}


def compare(path: str | Path) -> str:
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()] if Path(path).is_file() else []
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["goal"], []).append(r)
    lines = []
    for g, rs in by.items():
        last, prev = rs[-1], rs[-2] if len(rs) > 1 else None
        s = f"{g}: {'FINISHED' if last['finished'] else 'unfinished'} in {last['minutes']} min, {last['tokens']:,} tokens"
        if prev:
            worse = (prev["finished"] and not last["finished"]) or (last["finished"] and prev["finished"] and last["tokens"] > 1.5 * prev["tokens"])
            s += f"  (before: {'FINISHED' if prev['finished'] else 'unfinished'}, {prev['minutes']} min, {prev['tokens']:,} tokens)" \
                 + ("  REGRESSION" if worse else "")
        lines.append(s)
    return "\n".join(lines) or "no results yet"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("goals")
    r.add_argument("--config")
    r.add_argument("--only")
    r.add_argument("--max-minutes", type=float, default=45.0)
    r.add_argument("--results", default="bench/results.jsonl")
    c = sub.add_parser("compare")
    c.add_argument("results", nargs="?", default="bench/results.jsonl")
    a = ap.parse_args(argv)
    if a.cmd == "compare":
        print(compare(a.results))
        return 0
    specs = yaml.safe_load(Path(a.goals).read_text(encoding="utf-8"))
    out = Path(a.results)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orca-bench-") as tmp:
        for s in specs:
            if a.only and s["name"] != a.only:
                continue
            res = run_goal(s, tmp, a.config, max_minutes=a.max_minutes)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(res) + "\n")
            print(json.dumps(res))
    print(compare(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
