import json

import pytest

from orchestrator import Store
from orchestrator.agent import PROTOCOL, parse_reply, run_ticket
from orchestrator.providers import MockProvider
from orchestrator.tools import ToolBox


def j(**kw):
    return json.dumps(kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def mk(store):
    return store.create_ticket(dict(
        title="T", assignee="w", worker_role="r", context="c", acceptance_criteria=["a"],
        return_spec={"format": "f", "description": "d"}), actor="m")["id"]


TOOL = j(action="tool", tool="search_registry", args={"query": "x"})


def test_invalid_replies_must_be_consecutive_to_escalate(store, tmp_path):
    tid = mk(store)
    # three invalid replies in total, but separated by valid tool calls: used to escalate, must now succeed
    p = MockProvider(["junk", TOOL, "junk", TOOL, "junk", j(action="done", result={"ok": 1})])
    t = run_ticket(store, p, tid, "w", "role", ["m"], capability_retry_limit=3, toolbox=ToolBox(tmp_path, "w"))
    assert t["status"] == "done"


def test_three_consecutive_invalid_replies_escalate_and_are_logged(store):
    tid = mk(store)
    long_bad = '{"action": "done", "result": {"report": "line one\n"unterminated'
    t = run_ticket(store, MockProvider([long_bad, "nope", "still nope"]), tid, "w", "role", ["m"],
                   capability_retry_limit=3)
    assert t["status"] == "escalated"
    bad = [e for e in store.events(tid) if e["type"] == "invalid_reply"]
    assert [e["payload"]["n"] for e in bad] == [1, 2, 3]
    assert bad[0]["payload"]["head"].startswith('{"action": "done"') and bad[0]["payload"]["chars"] == len(long_bad)


def test_model_chain_resets_after_a_valid_reply(store, tmp_path):
    tid = mk(store)
    p = MockProvider(["junk", TOOL, j(action="done", result={})])
    run_ticket(store, p, tid, "w", "role", ["small", "big"], toolbox=ToolBox(tmp_path, "w"))
    assert [c["model"] for c in p.calls] == ["small", "big", "small"]


def test_raw_newlines_and_tabs_inside_strings_are_accepted():
    text = 'Here you go:\n```json\n{"action": "done", "result": {"report": "line one\nline two\tindented"}}\n```'
    r = parse_reply(text)
    assert r["result"]["report"] == "line one\nline two\tindented"


def test_protocol_tells_workers_to_keep_replies_small():
    assert "write_file" in PROTOCOL and "SMALL" in PROTOCOL


def test_a_bare_string_result_is_wrapped_not_a_crash(store):
    tid = mk(store)
    t = run_ticket(store, MockProvider([j(action="done", result="# Report\nfull markdown here")]), tid, "w", "r", ["m"])
    assert t["status"] == "done" and t["result"] == {"text": "# Report\nfull markdown here"}
    t2 = run_ticket(store, MockProvider([j(action="done", result=["a", "b"])]), mk(store), "w", "r", ["m"])
    assert t2["result"] == {"value": ["a", "b"]}


def test_manager_can_finish_with_a_string_result(store, tmp_path):
    from orchestrator.config import load_config
    from orchestrator.manager import Manager
    m = Manager("research_manager", store, MockProvider([j(action="done", result="plain text report")]),
                load_config(None), tmp_path, "role", ["m"], ["w"])
    top = store.create_ticket(dict(title="T", assignee="research_manager", worker_role="w", context="c",
                                   acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"}),
                              actor="top_manager")["id"]
    t = m.work(top)
    assert t["status"] == "done" and t["result"] == {"text": "plain text report"}
