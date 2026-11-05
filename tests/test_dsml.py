import json

import pytest

from orchestrator import Store
from orchestrator.agent import parse_reply, run_ticket
from orchestrator.config import load_config
from orchestrator.dsml import has_dsml, parse_dsml
from orchestrator.manager import Manager
from orchestrator.providers import MockProvider
from orchestrator.tools import ToolBox

B = "｜｜"            # the full-width bars DeepSeek puts around DSML tags


def tag(name, attrs="", close=False):
    return f"<{'/' if close else ''}{B}DSML{B} {name}{(' ' + attrs) if attrs else ''}>"


# Shapes observed in a live DeepSeek run
ARGS_STYLE = ("\n\n" + tag("calls") + "\n" + tag("invoke", 'name="tool" string="true"') + "write_file" +
              tag("parameter", close=True) + "\n" + tag("parameter", 'name="args" string="false"') +
              '{"path": "extract.py", "content": "import json\\nd=json.load(open(\'raw.json\'))\\nif a < b: pass"}' +
              tag("parameter", close=True) + "\n" + tag("invoke", close=True) + "\n" + tag("calls", close=True))

NAMED_TOOL_ARGS = (tag("calls") + tag("invoke", 'name="write_file"') + tag("parameter", 'name="args" string="false"') +
                   '{"path": "a.py", "content": "x = 1"}' + tag("parameter", close=True) + tag("invoke", close=True) +
                   tag("calls", close=True))

SEPARATE_PARAMS = (tag("calls") + tag("invoke", 'name="run_python"') +
                   tag("parameter", 'name="script" string="true"') + "work/a.py" + tag("parameter", close=True) +
                   tag("parameter", 'name="timeout" string="false"') + "30" + tag("parameter", close=True) +
                   tag("invoke", close=True) + tag("calls", close=True))

SEARCH = (tag("calls") + tag("invoke", 'name="search_registry"') +
          tag("parameter", 'name="query" string="true"') + "serial port" + tag("parameter", close=True) +
          tag("invoke", close=True) + tag("calls", close=True))

TWO_CALLS = ARGS_STYLE.replace(tag("calls", close=True), "") + SEARCH


def test_each_observed_shape_becomes_a_normal_tool_call():
    r = parse_dsml(ARGS_STYLE)
    assert r["action"] == "tool" and r["tool"] == "write_file"
    assert r["args"]["path"] == "extract.py" and "if a < b" in r["args"]["content"]       # code with '<' survives
    assert "\n" in r["args"]["content"]                                                   # escaped newlines decoded
    assert parse_dsml(NAMED_TOOL_ARGS) == {"action": "tool", "tool": "write_file", "args": {"path": "a.py", "content": "x = 1"}}
    assert parse_dsml(SEPARATE_PARAMS) == {"action": "tool", "tool": "run_python", "args": {"script": "work/a.py", "timeout": 30}}
    assert parse_dsml(SEARCH) == {"action": "tool", "tool": "search_registry", "args": {"query": "serial port"}}


def test_only_the_first_of_several_calls_is_used():
    r = parse_dsml(TWO_CALLS)
    assert r["tool"] == "write_file"


def test_garbage_and_non_dsml_are_not_misread():
    assert parse_dsml("just some prose") is None
    assert parse_dsml("\n\n<｜动作｜>") is None          # the stray special token seen live
    assert parse_dsml(tag("calls") + tag("calls", close=True)) is None   # no invoke inside
    assert not has_dsml("if a < b: print('DSML is a word here')")
    assert has_dsml(ARGS_STYLE)


def test_dsml_is_only_accepted_when_asked_for_so_managers_are_unaffected():
    assert parse_reply(ARGS_STYLE) is None                                # default: invalid, as before
    assert parse_reply(ARGS_STYLE, allow_dsml=True)["tool"] == "write_file"
    assert parse_reply('{"action": "done"}', allow_dsml=True) == {"action": "done"}     # real JSON still wins
    assert parse_reply(ARGS_STYLE, key="say", allow_dsml=True) is None


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def mk(store):
    return store.create_ticket(dict(title="T", assignee="w", worker_role="r", context="c", acceptance_criteria=["a"],
                                    return_spec={"format": "f", "description": "d"}), actor="m")["id"]


def test_worker_runs_a_dsml_tool_call_without_wasting_a_retry(store, tmp_path):
    tid = mk(store)
    p = MockProvider([ARGS_STYLE, json.dumps({"action": "done", "result": {"ok": 1}})])
    t = run_ticket(store, p, tid, "w", "role", ["m"], toolbox=ToolBox(tmp_path, "w", store=store, ticket_id=tid))
    assert t["status"] == "done"
    assert (tmp_path / "extract.py").exists()                                              # the call really ran
    types = [e["type"] for e in store.events(tid)]
    assert "invalid_reply" not in types and "reply_normalized" in types and types.count("tool_call") == 1
    assert len(p.calls) == 2                                                                # no extra nudge round trip
    assert "<|...|>" in p.calls[0]["system"] and "DSML" in p.calls[0]["system"]             # prompt forbids the markup


# ---------------------------------------------------------- manager worker cap

def test_manager_hitting_the_worker_cap_gets_feedback_instead_of_crashing(store, tmp_path):
    cfg = load_config(None)
    cfg["policies"]["max_children_per_agent"] = 2
    ws = dict(title="W", worker_role="r", context="c", acceptance_criteria=["a"],
              return_spec={"format": "f", "description": "d"})
    p = MockProvider([
        json.dumps({"action": "delegate", "tickets": [ws, ws]}),                            # fills the cap
        json.dumps({"action": "done", "result": {"w": 1}}), json.dumps({"action": "done", "result": {"w": 2}}),
        json.dumps({"action": "delegate", "tickets": [ws]}),                                # tries a third: rejected
        json.dumps({"action": "done", "result": {"final": "used what we had"}}),            # told to finish instead
    ])
    m = Manager("research_manager", store, p, cfg, tmp_path, "role", ["m"], ["w"])
    top = store.create_ticket({**ws, "assignee": "research_manager", "worker_role": "worker"}, "director")["id"]
    t = m.work(top)
    assert t["status"] == "done" and t["result"] == {"final": "used what we had"}
    final_prompt = p.calls[-1]["messages"][0]["content"]
    assert "# IMPORTANT" in final_prompt and "rejected" in final_prompt and "yourself with your tools" in final_prompt
    assert "delegation rejected" in m.memory.read_journal()
    assert len(store.list_tickets(parent_id=top)) == 2
