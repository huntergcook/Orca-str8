"""Tolerant reader for DeepSeek's native tool-call markup ("DSML").

DeepSeek models are trained to emit tool calls as tags like

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="write_file">
    <｜｜DSML｜｜ parameter name="args" string="false">{"path": "a.py", "content": "..."}</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

and sometimes leak it into a plain-text reply instead of the JSON our protocol asks for. Observed variants
(from a live run): `invoke name="<tool>"` with an `args` JSON parameter or with separate parameters, and
`invoke name="tool"` with the tool's name as the tag's text. Closing tags are often mismatched, so this reads
the tags as a flat sequence instead of matching pairs. Only the first call is returned: the runner handles one
action per turn and the model re-issues the rest.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Any DSML tag: opening or closing, with or without the full-width bars the models emit.
_TAG = re.compile(r"<\s*(/?)\s*[｜|]*\s*DSML\s*[｜|]*\s*([A-Za-z_]+)([^>]*)>")
_ATTR = re.compile(r'([A-Za-z_]+)\s*=\s*"([^"]*)"')


def has_dsml(text: str) -> bool:
    return "DSML" in text and _TAG.search(text) is not None


def _loads(value: str) -> Any:
    try:
        return json.loads(value, strict=False)
    except (json.JSONDecodeError, ValueError):
        return value


def parse_dsml(text: str) -> dict[str, Any] | None:
    """The first tool call in DSML markup as {"action": "tool", "tool": ..., "args": {...}}, or None."""
    tags = list(_TAG.finditer(text))
    invoke_i = next((i for i, m in enumerate(tags) if not m.group(1) and m.group(2) == "invoke"), None)
    if invoke_i is None:
        return None
    invoke = tags[invoke_i]
    attrs = dict(_ATTR.findall(invoke.group(3)))
    name = attrs.get("name", "").strip()

    def text_after(i: int) -> str:                       # text between tag i and the next tag
        end = tags[i + 1].start() if i + 1 < len(tags) else len(text)
        return text[tags[i].end():end].strip()

    params: dict[str, Any] = {}
    for i in range(invoke_i + 1, len(tags)):
        m = tags[i]
        if m.group(1) or m.group(2) != "parameter":
            if not m.group(1) and m.group(2) == "invoke":
                break                                    # a second call starts here: ignore it
            continue
        pattrs = dict(_ATTR.findall(m.group(3)))
        pname = pattrs.get("name", "").strip()
        raw = text_after(i)
        if pname:
            params[pname] = raw if pattrs.get("string") == "true" else _loads(raw)

    tool = name
    if name in ("tool", ""):                            # <invoke name="tool">write_file</...>: the name is the text
        tool = text_after(invoke_i) or str(params.pop("tool", "")).strip()
    if not tool:
        return None
    if isinstance(params.get("args"), str):
        params["args"] = _loads(params["args"])
    args = params["args"] if isinstance(params.get("args"), dict) else {k: v for k, v in params.items() if k != "tool"}
    return {"action": "tool", "tool": tool, "args": args}
