"""The mission brief: a questionnaire the human fills out before the harness is ever used.

It is the project's charter. Its answers are injected verbatim into every agent's prompt (see
config.project_rules), so the goal, the success criteria and the operating rules survive restarts and
compaction. Only the human edits it (`ctl intake --answer`, or a text editor); agents cannot, because it lives in
the project's config/ folder and agent file tools are confined to the workspace.

    config/mission.yaml     questions: [{id, ask, hint, required, answer}]

Unanswered required questions block new work (`ctl create`, the daemon), and the director is told to ask them,
one to three at a time, and to push back on vague answers.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from .util import atomic_write

HEADER = """\
# MISSION BRIEF - fill this in before the first run.
#
# Every agent in the harness (director, managers, workers, verifiers) is given your answers below, word for word,
# on every call. It is how the harness remembers what you actually want after restarts and long runs.
# Fill in `answer:` for each question. Be specific; vague answers will be questioned by the director.
# Questions marked `required: true` must be answered before any work starts.
# Lists are fine: write `answer: ["first", "second"]` or use YAML list syntax.
#
"""

QUESTIONS: list[dict[str, Any]] = [
    {"id": "goal", "required": True,
     "ask": "What is the goal, in a sentence or two?",
     "hint": "The outcome you want, not the method. Example: 'Skyrim VR holds 90 fps in Whiterun on my headset.'"},
    {"id": "why", "required": False,
     "ask": "Why does it matter, and what will you do with the result?",
     "hint": "Helps the harness judge trade-offs when the goal is ambiguous."},
    {"id": "success_criteria", "required": True,
     "ask": "How will we know the goal is met? List each criterion.",
     "hint": "Concrete and checkable. 'Tests pass', 'frame time under 11 ms in scene X', 'report covers A, B, C'."},
    {"id": "success_checks", "required": False,
     "ask": "Which shell commands exit 0 exactly when a criterion is met?",
     "hint": "Machine checks let the harness finish (or keep going) without asking anyone. Run from the project folder. "
             "Example: ['python -m pytest -q', 'python tools/bench.py --max-ms 11']. Leave empty if none are possible; "
             "the verifiers then judge the criteria."},
    {"id": "not_goals", "required": False,
     "ask": "What is explicitly out of scope?",
     "hint": "Things that look related but you do not want done."},
    {"id": "hard_constraints", "required": False,
     "ask": "What must never happen?",
     "hint": "Destructive actions, files or machines that are off limits, licenses you cannot accept, spending limits."},
    {"id": "rigor", "required": False,
     "ask": "How rigorous should the work be: quick_and_dirty, balanced, or rigorous?",
     "hint": "Quick prototypes are not gold-plated; critical results are not waved through."},
    {"id": "time_and_cost", "required": False,
     "ask": "Any deadline or money limit?",
     "hint": "e.g. 'finish within 3 days', '$40 total', 'unlimited on subscription CLIs but no API spend'."},
    {"id": "autonomy", "required": False,
     "ask": "When you are not around, should the harness stop and wait for you, or make its best assumption and keep going?",
     "hint": "Recommended: 'assume_and_continue' for everything except safety, spending and refusals. Say what you want."},
    {"id": "decision_authority", "required": False,
     "ask": "How much may the director decide and speak for you? What must still be yours?",
     "hint": "Default: the director makes every strategic call (scope trade-offs, approach, priorities, accepting results, "
             "answering departments) as you would, from this brief, and records each in DECISIONS.md. "
             "List anything reserved for you, e.g. spending over $X, deleting data, publishing."},
    {"id": "interrupt_only_for", "required": False,
     "ask": "What is important enough to notify you about immediately?",
     "hint": "Examples: safety issue, budget about to run out, a decision only you can make, goal met."},
    {"id": "failure_behavior", "required": False,
     "ask": "When something fails repeatedly (a worker, a tool, a model), what should happen?",
     "hint": "e.g. 'try another model, then another approach, then split it up, never give up before 5 distinct approaches'."},
    {"id": "model_access", "required": False,
     "ask": "Which models can the harness use, and what are their limits?",
     "hint": "CLIs (claude, codex) and their plan limits, API keys and budgets, local models (Ollama), GPUs."},
    {"id": "director_cli", "required": False,
     "ask": "Which CLI or model should play the director, and which is the fallback if it hits its usage limit?",
     "hint": "e.g. 'claude first, codex as fallback'."},
    {"id": "machine_resources", "required": False,
     "ask": "What hardware and machines can the harness use?",
     "hint": "CPU cores, RAM, GPUs, other PCs it may log into, devices (headsets, boards) and how to keep them safe."},
    {"id": "reuse_policy", "required": False,
     "ask": "How hard should it lean on existing open-source work, and which licenses are acceptable?",
     "hint": "Default: search registry, then GitHub/PyPI/docs, and build only when nothing fits. Say if you want stricter."},
    {"id": "existing_work", "required": False,
     "ask": "What already exists that it should read first?",
     "hint": "Repos, folders, notes, prior attempts, links. Add read-only folders to `read_roots` in the config."},
    {"id": "safety_limits", "required": False,
     "ask": "Hardware or safety limits the harness must respect?",
     "hint": "Voltage, temperature, anything physical. These go to you, never around you."},
    {"id": "reporting", "required": False,
     "ask": "How and how often should it report to you, and in how much detail?",
     "hint": "e.g. 'phone notification via ntfy topic X for blockers; a short daily digest; details only when I ask'."},
    {"id": "self_improvement", "required": False,
     "ask": "Is the harness itself part of the work? May it change its own code?",
     "hint": "If yes: which branch, which checks must pass, and whether changes need your approval before taking effect."},
    {"id": "model_guidance", "required": False,
     "ask": "When should which model be used?",
     "hint": "Plain-language guidance for choosing models per role or task (strongest for planning, cheap for bulk reading...)."},
    {"id": "anything_else", "required": False,
     "ask": "Anything else the harness should know about how you work or what you care about?",
     "hint": ""},
]


# orca.yaml (the project's single human-edited file) uses friendlier names than the questionnaire ids
FRIENDLY = {"goal": "goal", "done_when": "success_criteria", "checks": "success_checks", "never": "hard_constraints",
            "quality": "rigor", "when_away": "autonomy", "notes": "anything_else"}
DEFAULT_ANSWERS = {"rigor": "balanced", "autonomy": "assume_and_continue"}


def _blank(answer: Any) -> bool:
    if answer is None:
        return True
    if isinstance(answer, str):
        return not answer.strip()
    if isinstance(answer, (list, dict, tuple)):
        return not answer
    return False


def path_for(cfg: dict[str, Any]) -> Path:
    name = cfg.get("mission_file") or "config/mission.yaml"
    p = Path(name)
    return p if p.is_absolute() else Path.cwd() / p


def template_data() -> dict[str, Any]:
    return {"version": 1, "questions": [{**q, "answer": ""} for q in QUESTIONS]}


def _dump(data: dict[str, Any]) -> str:
    return HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def scaffold(path: str | Path, overwrite: bool = False) -> Path:
    """Write a blank mission brief for the human to fill in. Never overwrites an existing one by default."""
    p = Path(path)
    if p.exists() and not overwrite:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, _dump(template_data()))
    return p


def load(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(data.get("mission"), dict):                 # orca.yaml: one file for everything
        ans = {qid: data["mission"].get(k) for k, qid in FRIENDLY.items()}
        ans["model_guidance"] = (data.get("models") or {}).get("guide")
        data = {"version": 1, "source": "orca", "questions": [{**q, "answer": ans.get(q["id"]) if ans.get(q["id"]) is not None else ""}
                                                              for q in QUESTIONS]}
    known = {q["id"]: q for q in data.get("questions") or [] if isinstance(q, dict) and "id" in q}
    for q in QUESTIONS:                      # questions added by a newer version of the harness appear as unanswered
        known.setdefault(q["id"], {**q, "answer": ""})
    data["questions"] = list(known.values())
    return data


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Where the mission brief stands: does it exist, and which required questions are still unanswered."""
    p = path_for(cfg)
    data = load(p)
    if data is None:
        return {"path": str(p), "exists": False, "complete": False, "unanswered_required": [], "unanswered_optional": [],
                "answered": []}
    qs = data["questions"]
    return {"path": str(p), "exists": True,
            "complete": not any(q.get("required") and _blank(q.get("answer")) for q in qs),
            "unanswered_required": [{"id": q["id"], "ask": q.get("ask", ""), "hint": q.get("hint", "")}
                                    for q in qs if q.get("required") and _blank(q.get("answer"))],
            "unanswered_optional": [q["id"] for q in qs if not q.get("required") and _blank(q.get("answer"))],   # ids only: defaults apply
            "answered": [q["id"] for q in qs if not _blank(q.get("answer"))]}


def gate_error(cfg: dict[str, Any]) -> str | None:
    """Why new work must not start yet, or None. Only applies to projects that have a mission brief, so projects
    set up before it existed keep working."""
    st = status(cfg)
    if not st["exists"] or st["complete"]:
        return None
    ids = ", ".join(q["id"] for q in st["unanswered_required"])
    return (f"the mission brief ({st['path']}) is incomplete: unanswered required questions: {ids}. Ask the human "
            "(`ctl intake` lists them), record each answer with `ctl intake --answer ID TEXT`, then try again.")


def set_answer(cfg: dict[str, Any], question_id: str, answer: Any) -> dict[str, Any]:
    p = path_for(cfg)
    data = load(p)
    if data is None:
        scaffold(p)
        data = load(p)
    assert data is not None
    if data.get("source") == "orca":
        key = next((k for k, qid in FRIENDLY.items() if qid == question_id), None)
        if key is None:
            raise KeyError(f"{question_id!r} is not a mission field in orca.yaml (fields: {sorted(FRIENDLY)}; edit models.guide there for model guidance)")
        _orca_set(p, key, answer)
        return status(cfg)
    for q in data["questions"]:
        if q["id"] == question_id:
            q["answer"] = answer
            break
    else:
        raise KeyError(f"no question {question_id!r}; known: {[q['id'] for q in data['questions']]}")
    atomic_write(p, _dump(data))
    return status(cfg)


def _orca_set(path: Path, key: str, value: Any) -> None:
    """Change one `mission:` field in orca.yaml in place, keeping every comment and the rest of the file untouched."""
    import json
    import re
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^mission:\s*(#.*)?$", ln))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "#"))), len(lines))
    new_val = json.dumps(value, ensure_ascii=False)
    for i in range(start + 1, end):
        if re.match(rf"^  {key}:(\s|$)", lines[i]):
            comment = ""
            if lines[i].count('"') % 2 == 0 and lines[i].count("'") % 2 == 0:
                m = re.search(r"\s+#.*$", lines[i])
                comment = m.group(0) if m else ""
            j = i + 1
            while j < end and (lines[j].startswith("    ") or lines[j].startswith("  - ")):
                j += 1
            lines[i:j] = [f"  {key}: {new_val}{comment}"]
            break
    else:
        lines.insert(start + 1, f"  {key}: {new_val}")
    atomic_write(path, "\n".join(lines) + "\n")


def render_charter(cfg: dict[str, Any], max_chars: int = 6000) -> str:
    """The answered questions as plain text, for injection into prompts. Empty if there is no brief."""
    data = load(path_for(cfg))
    if data is None:
        return ""
    lines = ["# Mission brief (written by the human; binding; never contradict it)"]
    for q in data["questions"]:
        a = q.get("answer")
        if _blank(a):
            continue
        if isinstance(a, (list, tuple)):
            a = "\n".join(f"  - {x}" for x in a)
            lines.append(f"## {q.get('ask', q['id'])}\n{a}")
        else:
            lines.append(f"## {q.get('ask', q['id'])}\n{str(a).strip()}")
    if len(lines) > 1:
        for qid, default in DEFAULT_ANSWERS.items():
            q = next((x for x in data["questions"] if x["id"] == qid), None)
            if q is not None and _blank(q.get("answer")):
                lines.append(f"## {q.get('ask', qid)}\n{default} (default)")
    text = "\n".join(lines)
    return text[:max_chars] if len(lines) > 1 else ""


def charter_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(render_charter(cfg, 10**6).encode("utf-8")).hexdigest()[:12]


def success_checks(cfg: dict[str, Any]) -> list[str]:
    data = load(path_for(cfg))
    if data is None:
        return []
    for q in data["questions"]:
        if q["id"] == "success_checks":
            a = q.get("answer")
            if isinstance(a, str):
                a = [ln.strip() for ln in a.splitlines() if ln.strip()]
            return [str(x) for x in (a or []) if str(x).strip()]
    return []


def answer_of(cfg: dict[str, Any], question_id: str) -> Any:
    data = load(path_for(cfg))
    for q in (data or {}).get("questions", []):
        if q["id"] == question_id:
            return q.get("answer")
    return None
