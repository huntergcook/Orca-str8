"""Dependencies and critical path, drift review, learnings log, tracing."""
import json

import pytest

from orchestrator import critical_path, learnings
from orchestrator.ctl import Ctl
from orchestrator.providers import MockProvider
from orchestrator.trace import Tracer, to_span

from test_daemon import TICKET, cfg_for, j


def mk(tmp_path, script=None, **pol):
    cfg = cfg_for(tmp_path)
    cfg["policies"].update(pol)
    return Ctl(cfg, cfg["workspace"], MockProvider(script or [])), cfg


def test_depends_on_gates_and_orders(tmp_path):
    ctl, _ = mk(tmp_path)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    b = d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    c = d.create_ticket(dict(TICKET, title="C", depends_on=[b]))
    with pytest.raises(Exception):
        d.create_ticket(dict(TICKET, title="bad", depends_on=["nope"]))
    ana = critical_path.analyze(ctl.store)
    assert [x["id"] for x in ana["critical_chain"]] == [a, b, c]
    assert ana["serial"] and "SERIAL" in ana["message"]
    assert not critical_path.deps_met(ctl.store, ctl.store.get_ticket(b))


def test_independent_work_is_not_serial(tmp_path):
    ctl, _ = mk(tmp_path)
    for t in "XY":
        ctl.director.create_ticket(dict(TICKET, title=t))
    ana = critical_path.analyze(ctl.store)
    assert not ana["serial"] and ana["ready"] == 2
    assert "critical_path" in ctl.board()


def test_failed_dependency_escalates_to_director(tmp_path):
    ctl, _ = mk(tmp_path)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    b = d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    ctl.store.transition(a, "in_progress", "x")
    ctl.store.transition(a, "failed", "x", note="boom")
    assert d._deps_ready(ctl.store.get_ticket(b)) is False
    assert ctl.store.get_ticket(b)["status"] == "escalated"


def test_serial_bottleneck_event(tmp_path):
    ctl, _ = mk(tmp_path, serial_minutes=1, drift_review_minutes=0)
    d = ctl.director
    a = d.create_ticket(dict(TICKET, title="A"))
    d.create_ticket(dict(TICKET, title="B", depends_on=[a]))
    seen = []
    d.notify = lambda *x: seen.append(x)
    d.oversight()
    d._serial = {k: v - 3600 for k, v in d._serial.items()}
    d._oversight_at = 0
    d.oversight()
    assert any(e["type"] == "serial_bottleneck" for e in ctl.store.events(None)) and seen


def test_drift_review_reaches_director_prompt(tmp_path):
    ctl, _ = mk(tmp_path, [j(on_track=False, off_charter=["t1"], missing=["crit"], advice="cancel t1")])
    res = ctl.director.drift_review()
    assert not res["on_track"]
    assert "cancel t1" in ctl.director._system_prompt()


def test_charter_change_detected(tmp_path):
    from orchestrator import mission
    ctl, cfg = mk(tmp_path, drift_review_minutes=0)
    ctl.director.oversight()
    mission.set_answer(cfg, "goal", "something else")
    ctl.director._oversight_at = 0
    ctl.director.oversight()
    assert any(e["type"] == "charter_changed" for e in ctl.store.events(None))


def test_learnings_roundtrip(tmp_path):
    n = learnings.record(tmp_path, "t1", "Try fix", {"dead_ends": ["approach A fails"], "learned": "B works",
                                                      "outcome": "refuted", "summary": "no"}, {"hypothesis": "A helps"})
    assert n == 3
    s = learnings.section(tmp_path)
    assert "DEAD END" in s and "EXPERIMENT REFUTED" in s


def test_trace_file_and_span(tmp_path):
    ctl, cfg = mk(tmp_path)
    tr = Tracer({"trace": {"file": True}}, tmp_path)
    ctl.store.tracer = tr
    ctl.director.create_ticket(dict(TICKET))
    lines = (tmp_path / "traces.jsonl").read_text().splitlines()
    span = json.loads(lines[0])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    assert to_span({"seq": 1, "ts": "2026-01-01T00:00:00+00:00", "actor": "a", "type": "t", "payload": {}}, None)["name"] == "a:t"
