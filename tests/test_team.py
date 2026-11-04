"""The orchestrator behaves like a team: parallel work, live status, follow-ups, cross-department requests."""
import json
import threading
import time

import pytest

from orchestrator import Store
from orchestrator.agent import SHORTENED, compact, run_ticket
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.limits import GuardedProvider, Halted, estimate_cost
from orchestrator.manager import Manager
from orchestrator.providers import Completion
from orchestrator.tools import ToolBox

SPEC = dict(worker_role="r", context="c", acceptance_criteria=["a"], return_spec={"format": "f", "description": "d"})


def j(**kw):
    return json.dumps(kw)


class Probe:
    """A fake model that answers by situation and measures how many calls are in flight at once."""

    def __init__(self, delay=0.3, worker=None, lead=None):
        self.delay, self.lock = delay, threading.Lock()
        self.active = self.max_active = 0
        self.calls = []
        self.worker = worker or (lambda first, msgs: j(action="done", result={"summary": "ok"}))
        self.lead = lead or default_lead

    def complete(self, *, model, system, messages, max_tokens=16000):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append({"model": model, "system": system, "messages": messages})
        try:
            time.sleep(self.delay)
            first = messages[0]["content"]
            is_manager = "YOU OWN WORKER PROBLEMS" in system
            text = self.lead(first, messages) if is_manager else self.worker(first, messages)
            return Completion(text=text, model=model, input_tokens=1000, output_tokens=50, cache_hit_tokens=400)
        finally:
            with self.lock:
                self.active -= 1


def default_lead(first, messages):
    if "Plan the work" in first:
        return j(action="delegate", tickets=[{**SPEC, "title": f"part {i}"} for i in range(4)])
    return j(action="done", result={"report": "all parts done", "follow_ups": ["write the user guide"]})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def top(store, assignee="research_manager", title="Survey"):
    return store.create_ticket({**SPEC, "title": title, "assignee": assignee, "worker_role": "worker"}, "top_manager")["id"]


# ------------------------------------------------------------------------- parallelism

def test_workers_in_a_department_run_in_parallel(store, tmp_path):
    p = Probe(delay=0.4)
    m = Manager("research_manager", store, p, load_config(None), tmp_path, "role", ["m"], ["w"])
    t0 = time.monotonic()
    assert m.work(top(store))["status"] == "done"
    elapsed = time.monotonic() - t0
    assert p.max_active >= 3                                     # 4 workers, pool of 4: they overlapped
    assert elapsed < 0.4 * 6                                     # sequential would be ~6 calls x 0.4s


def test_departments_run_in_parallel_through_the_top_manager(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}, "tooling": {}, "verification": {}}
    p = Probe(delay=0.4, lead=lambda first, msgs: j(action="done", result={"report": "done directly"}))
    ctl = Ctl(cfg, tmp_path / "ws", p)
    for dept in ("research", "tooling", "verification"):
        ctl.create(dict(department=dept, title=f"{dept} job", context="c", acceptance_criteria=["a"],
                        return_spec={"format": "f", "description": "d"}))
    t0 = time.monotonic()
    out = ctl.run()
    assert all(t["status"] == "done" for t in out["tickets"])
    assert p.max_active == 3 and time.monotonic() - t0 < 0.4 * 3     # all three managers worked at the same time
    ctl.close()


def test_the_concurrency_cap_limits_calls_in_flight(store, tmp_path):
    cfg = load_config(None)
    cfg["policies"]["max_concurrent_calls"] = 2
    p = Probe(delay=0.3)
    g = GuardedProvider(p, store, cfg, tmp_path)
    m = Manager("research_manager", store, g, cfg, tmp_path, "role", ["m"], ["w"])
    assert m.work(top(store))["status"] == "done"
    assert p.max_active == 2


def test_a_pause_stops_a_parallel_run_cleanly(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    paused = []

    def worker(first, msgs):
        if not paused:
            (tmp_path / "ws" / "PAUSE").write_text("stop")                # the human pulls the brake mid-run
            paused.append(1)
        return j(action="done", result={})

    ctl = Ctl(cfg, tmp_path / "ws", Probe(delay=0.05, worker=worker))
    ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                    return_spec={"format": "f", "description": "d"}))
    out = ctl.run()
    assert "paused" in out["halted"]
    assert not (tmp_path / "ws" / "RUNNING").exists()
    ctl.close()


# --------------------------------------------------------------------------- live status

def test_every_manager_and_worker_has_a_live_status_and_its_usage(store, tmp_path):
    m = Manager("research_manager", store, Probe(delay=0.01), load_config(None), tmp_path, "role", ["m"], ["w"])
    tid = top(store)
    m.work(tid)
    agents = {a["agent_id"]: a for a in store.list_agents()}
    mgr = agents[f"research_manager@{tid}"]
    assert mgr["kind"] == "manager" and mgr["department"] == "research" and mgr["state"] == "done"
    workers = [a for a in agents.values() if a["kind"] == "worker"]
    assert len(workers) == 4 and all(w["state"] == "done" and w["department"] == "research" for w in workers)
    assert all(w["calls"] == 1 and w["tokens_in"] == 1000 and w["tokens_out"] == 50 for w in workers)
    assert {w["name"] for w in workers} == {"worker:r-1", "worker:r-2", "worker:r-3", "worker:r-4"}


def test_status_reflects_what_an_agent_is_doing_right_now(store, tmp_path):
    seen = []

    class Spy(Probe):
        def complete(self, **kw):
            seen.append({a["agent_id"]: a["state"] for a in store.list_agents()})
            return super().complete(**kw)

    tid = store.create_ticket({**SPEC, "title": "w", "assignee": "worker:r-1"}, "m")["id"]
    p = Spy(delay=0, worker=lambda first, msgs: j(action="tool", tool="search_registry", args={"query": "x"})
            if sum(1 for m in msgs if m["role"] == "assistant") == 0 else j(action="done", result={}))
    run_ticket(store, p, tid, "worker:r-1", "role", ["m"], toolbox=ToolBox(tmp_path, "w"), department="research")
    assert seen[0][tid] == "thinking" and seen[1][tid] == "thinking"
    assert store.list_agents()[0]["state"] == "done"


def test_usage_is_broken_down_by_department(store, tmp_path):
    cfg = load_config(None)
    for dept in ("research", "software"):
        Manager(f"{dept}_manager", store, Probe(delay=0), cfg, tmp_path, "role", ["m"], ["w"]).work(top(store, f"{dept}_manager"))
    by = store.usage_by_department()
    assert set(by) == {"research", "software"}
    assert by["research"]["calls"] == 6 and by["research"]["input_tokens"] == 6000 and by["research"]["cache_hit_tokens"] == 2400
    store.add_event(None, "verifier", "usage", {"input_tokens": 5, "output_tokens": 1})     # older events still attribute
    assert store.usage_by_department()["verification"]["input_tokens"] == 5


def test_the_dashboard_shows_the_team_and_usage(store, tmp_path):
    from orchestrator.dashboard import build_state
    Manager("research_manager", store, Probe(delay=0), load_config(None), tmp_path, "role", ["m"], ["w"]).work(top(store))
    s = build_state(tmp_path / "t.db", load_config(None))
    assert len(s["agents"]) == 5 and all(a["ticket_title"] for a in s["agents"])
    assert s["usage_by_department"]["research"]["calls"] == 6


# ---------------------------------------------------------------------- worker limits

def test_workers_get_a_countdown_before_the_tool_limit(store, tmp_path):
    tid = store.create_ticket({**SPEC, "title": "w", "assignee": "worker:r-1"}, "m")["id"]
    p = Probe(delay=0, worker=lambda first, msgs: j(action="tool", tool="search_registry", args={"query": "x"}))
    t = run_ticket(store, p, tid, "w", "role", ["m"], toolbox=ToolBox(tmp_path, "w"), max_tool_steps=10)
    assert t["status"] == "escalated" and "used all 10 tool steps" in t["escalation"]["reason"]
    texts = [m["content"] for m in p.calls[-1]["messages"] if m["role"] == "user"]
    assert any("8 tool steps left" in x for x in texts) and any("3 tool steps left" in x for x in texts)
    assert any("That was your last tool step" in x for x in texts)


def test_long_conversations_are_compacted_but_the_newest_results_stay_whole():
    msgs = [{"role": "user", "content": "ticket"}]
    for i in range(10):
        msgs += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": "Tool result: " + str(i) * 10000}]
    out = compact(msgs, max_chars=60000, keep_last=4)
    results = [m["content"] for m in out if m["content"].startswith("Tool result: ")]
    assert all(r.endswith(SHORTENED) for r in results[:6]) and not any(r.endswith(SHORTENED) for r in results[6:])
    small = [{"role": "user", "content": "Tool result: x"}]
    assert compact(small) == small


def test_role_names_are_normalized(store, tmp_path):
    m = Manager("research_manager", store, Probe(delay=0), load_config(None), tmp_path, "role", ["m"], ["w"])
    t = top(store)
    m._delegate(t, {"tickets": [{**SPEC, "title": "a", "worker_role": "Research Worker"},
                                {**SPEC, "title": "b", "worker_role": "report writer!"}]})
    assert [k["assignee"] for k in store.list_tickets(parent_id=t)] == ["worker:research_worker-1", "worker:report_writer-1"]


# ------------------------------------------------------- speaking up, no holes, autonomy

def test_worker_follow_ups_reach_the_manager_and_its_follow_ups_reach_the_top_manager(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    p = Probe(delay=0, worker=lambda first, msgs: j(action="done", result={"summary": "ok",
                                                                             "follow_ups": ["the API docs are stale"]}))
    ctl = Ctl(cfg, tmp_path / "ws", p)
    ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                    return_spec={"format": "f", "description": "d"}))
    ctl.run()
    review = next(c for c in p.calls if "Worker results" in c["messages"][0]["content"])["messages"][0]["content"]
    assert "follow-ups raised: the API docs are stale" in review and "no holes" in review
    board = ctl.board()
    assert [f["item"] for f in board["follow_ups"]] == ["write the user guide"]
    w = ctl.watch(timeout=2, grace=0.1, poll=0.05)
    assert w["reason"] == "completed" and w["completed"][0]["follow_ups"] == ["write the user guide"]
    assert ctl.watch(timeout=2, grace=0.1, poll=0.05)["reason"] == "idle"         # reported once
    ctl.close()


def test_a_worker_can_file_work_for_another_department_and_it_runs_in_the_same_run(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}, "tooling": {}}
    filed = []

    def worker(first, msgs):
        if "serial logger" in first or filed:
            return j(action="done", result={"summary": "ok"})
        filed.append(1)
        return j(action="tool", tool="request_work", args={"department": "tooling", "title": "Build a serial logger",
                                                           "need": "research needs to capture serial traffic"})

    p = Probe(delay=0, worker=worker, lead=lambda first, msgs: j(action="done", result={"report": "done"})
              if "Worker results" in first else j(action="delegate", tickets=[{**SPEC, "title": "investigate"}]))
    ctl = Ctl(cfg, tmp_path / "ws", p)
    ctl.create(dict(department="research", title="t", context="c", acceptance_criteria=["a"],
                    return_spec={"format": "f", "description": "d"}))
    out = ctl.run()
    titles = {t["title"]: t for t in out["tickets"]}
    assert titles["Build a serial logger"]["assignee"] == "tooling_manager"
    assert titles["Build a serial logger"]["status"] == "done"                        # picked up in the same run
    req = ctl.store.get_ticket(titles["Build a serial logger"]["id"])
    assert req["cc"] == ["top_manager"] and "research needs to capture serial traffic" in req["context"]
    ctl.close()


def test_managers_use_their_own_tools_to_do_the_work_themselves(store, tmp_path):
    def hands_on(first, msgs):
        if sum(1 for m in msgs if m["role"] == "assistant") == 0:
            return j(action="tool", tool="write_file", args={"path": "reports/summary.md", "content": "# Done by the manager"})
        return j(action="done", result={"report_path": "reports/summary.md"})

    m = Manager("research_manager", store, Probe(delay=0, lead=hands_on), load_config(None), tmp_path, "role", ["m"], ["w"])
    assert m.work(top(store))["status"] == "done"
    assert (tmp_path / "reports" / "summary.md").read_text() == "# Done by the manager"
    assert store.list_tickets(parent_id=None)[0]["result"]["report_path"] == "reports/summary.md"


def test_every_agent_can_request_work_and_the_prompt_says_to_speak_up(store, tmp_path):
    m = Manager("research_manager", store, Probe(delay=0), load_config(None), tmp_path, "role", ["m"], ["w"],
                departments={"research": "research_manager", "software": "software_manager"})
    m.work(top(store))
    worker_system = next(c for c in m.provider.calls if "YOU OWN WORKER PROBLEMS" not in c["system"])["system"]
    assert "request_work" in worker_system and "SPEAK UP" in worker_system
    manager_system = next(c for c in m.provider.calls if "YOU OWN WORKER PROBLEMS" in c["system"])["system"]
    assert "FULL AUTHORITY" in manager_system and "NO HOLES" in manager_system and "request_work" in manager_system


# ----------------------------------------------------------------------------- cost

def test_cost_estimate_and_cost_budget(store, tmp_path):
    prices = {"m": {"input": 1.0, "output": 2.0, "cache_hit": 0.1}}
    by_model = {"m": {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_hit_tokens": 400_000}}
    assert estimate_cost(by_model, prices) == pytest.approx(0.6 + 0.04 + 1.0)
    assert estimate_cost(by_model, {}) is None
    cfg = load_config(None)
    cfg["prices"] = prices
    cfg["budget"]["max_cost_usd"] = 0.001
    store.record_usage(None, "x", "research", Completion(text="", model="m", input_tokens=2000, output_tokens=100))
    with pytest.raises(Halted, match="cost budget exhausted"):
        GuardedProvider(Probe(delay=0), store, cfg, tmp_path).complete(model="m", system="s", messages=[])


def test_the_time_budget_applies_per_run_not_to_idle_time_between_runs(store, tmp_path, monkeypatch):
    cfg = load_config(None)
    cfg["budget"]["max_minutes"] = 1
    import orchestrator.limits as limits
    clock = [1000.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock[0])
    g = GuardedProvider(Probe(delay=0), store, cfg, tmp_path)
    g.check()                                                       # a fresh run starts with its full time budget
    clock[0] += 61
    with pytest.raises(Halted, match="time budget"):
        g.check()
    GuardedProvider(Probe(delay=0), store, cfg, tmp_path).check()   # the next run starts fresh again
