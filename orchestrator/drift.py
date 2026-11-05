"""Goal-drift review: a separate, stateless model call that sees only the mission brief, the director's notes and the board
and answers one question: is the work still the shortest sensible path to the goal? It is independent of the director so the
director is not grading its own plan."""
from __future__ import annotations

from typing import Any

from .agent import parse_reply
from .config import model_chain, project_rules
from .providers import usable

PROMPT = """\
You review a long-running project for GOAL DRIFT. You see only the human's mission brief, the director's notes and the ticket
board. Judge whether the work is still the shortest sensible path to the goal and success criteria.
Reply with exactly one JSON object:
  {"on_track": true|false,
   "off_charter": ["tickets or activities that do not serve a success criterion or that the brief rules out"],
   "missing": ["success criteria nobody is working on"],
   "advice": "one or two sentences: what to cancel, add or change"}
Be strict and specific; cite ticket ids. Do not praise."""


def review(provider: Any, store: Any, cfg: dict[str, Any], director_state: str, board: str) -> dict[str, Any]:
    ver = list(cfg["verification"].get("verifier_models") or [])
    models = ver or model_chain(cfg, "manager")          # a different model than the director where one is configured
    avail = usable(models) or models
    system = f"{PROMPT}\n\n{project_rules(cfg)}"
    user = f"# Director's notes\n{director_state or '(none)'}\n\n# Ticket board\n{board}"
    c = provider.complete(model=avail[0], system=system, messages=[{"role": "user", "content": user}])
    store.record_usage(None, "drift_reviewer", "drift", c)
    reply = parse_reply(c.text, key="on_track")
    if not reply:
        return {"on_track": True, "off_charter": [], "missing": [], "advice": "", "unparsed": True}
    return {"on_track": bool(reply.get("on_track", True)), "off_charter": [str(x) for x in reply.get("off_charter") or []],
            "missing": [str(x) for x in reply.get("missing") or []], "advice": str(reply.get("advice", ""))}
