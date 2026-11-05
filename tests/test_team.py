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


# ------------------------------------------------------------------------- cancelling running work

def test_cancelling_a_running_ticket_stops_its_manager_and_workers(store, tmp_path):
    from orchestrator.actions import cancel
    tid = top(store)
    started = threading.Event()

    def slow_worker(first, msgs):
        started.set()
        return j(action="tool", tool="list_dir", args={"path": "."})   # keeps working forever unless stopped

    p = Probe(delay=0.05, worker=slow_worker)
    m = Manager("research_manager", store, p, load_config(None), tmp_path, "role", ["m"], ["w"])
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("t", m.work(tid)))
    th.start()
    assert started.wait(5)
    r = cancel(store, tid, "priorities changed", "top_manager")
    assert r["status"] == "cancelled" and len(r["also_cancelled"]) == 4         # the cascade reached every worker
    th.join(10)
    assert not th.is_alive() and out["t"]["status"] == "cancelled"
    assert all(c["status"] == "cancelled" for c in store.list_tickets(parent_id=tid))
    time.sleep(0.3)                                  # a call already past its cancel check may still finish
    n = len(p.calls)
    time.sleep(0.5)
    assert len(p.calls) == n                                                    # nobody keeps calling the model


# ------------------------------------------------------------------------- protocol slips are corrected, not escalated

def test_a_tool_name_used_as_the_action_is_run_as_that_tool(store, tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    seen = []

    def lead(first, msgs):
        if len(msgs) == 1:
            return j(action="list_dir", args={"path": "."})              # a tool name in "action"
        seen.append(msgs[-1]["content"])
        return j(action="done", result={"report": "ok"})

    m = Manager("research_manager", store, Probe(delay=0, lead=lead), load_config(None), tmp_path, "role", ["m"], ["w"])
    assert m.work(top(store))["status"] == "done"
    assert seen and seen[0].startswith("Tool result:") and '"ok": true' in seen[0]


def test_an_unknown_manager_action_gets_feedback_instead_of_an_escalation(store, tmp_path):
    calls = []

    def lead(first, msgs):
        calls.append(first)
        if len(calls) == 1:
            return j(action="ponder")
        assert "is not an action" in first
        return j(action="done", result={"report": "ok"})

    m = Manager("research_manager", store, Probe(delay=0, lead=lead), load_config(None), tmp_path, "role", ["m"], ["w"])
    assert m.work(top(store))["status"] == "done"


def test_claim_resource_can_wait_for_a_busy_resource(store, tmp_path):
    store.register_resource("rig", "safe any time")
    a = store.create_ticket({**SPEC, "title": "a", "assignee": "worker:r-1"}, "m")["id"]
    b = store.create_ticket({**SPEC, "title": "b", "assignee": "worker:r-2"}, "m")["id"]
    store.claim_resource("rig", a, "1m", "w1")
    box = ToolBox(tmp_path, "w2", store=store, ticket_id=b, can_use_hardware=True)
    assert box.call("claim_resource", {"name": "rig"})["ok"] is False                 # busy: fails at once without wait
    threading.Timer(0.5, lambda: store.release_resource("rig", a, "w1")).start()
    t0 = time.monotonic()
    r = box.call("claim_resource", {"name": "rig", "wait": 10})
    assert r["ok"] and r["claimed"] == "rig" and time.monotonic() - t0 < 6


# ------------------------------------------------------------------------- the top manager picks models per ticket

def test_models_chosen_on_a_ticket_apply_to_its_manager_and_all_workers_below(tmp_path):
    cfg = load_config(None)
    cfg["departments"] = {"research": {}}
    cfg["models"]["manager"] = {"provider": "deepseek", "model": "cheap"}
    cfg["models"]["worker"] = {"provider": "deepseek", "model": "cheap"}
    cfg["models"]["fallback_chain"] = [{"provider": "deepseek", "model": "strong"}]
    p = Probe(delay=0)
    ctl = Ctl(cfg, tmp_path / "ws", p)
    easy = ctl.create(dict(department="research", title="easy", context="c", acceptance_criteria=["a"],
                           return_spec={"format": "f", "description": "d"}))["ticket_id"]
    hard = ctl.create(dict(department="research", title="hard", context="c", acceptance_criteria=["a"],
                           return_spec={"format": "f", "description": "d"}))["ticket_id"]
    assert ctl.set_models(hard, manager="strong", worker="strong")["models"] == {"manager": "strong", "worker": "strong"}
    with pytest.raises(Exception, match="unknown model"):
        ctl.set_models(easy, worker="nonexistent")
    ctl.run()
    by_ticket = {}
    for c in p.calls:
        first = c["messages"][0]["content"]
        key = "hard" if hard in first else "easy"              # workers' prompts carry the parent id in the origin trace
        by_ticket.setdefault(key, set()).add(c["model"])
    assert by_ticket["hard"] == {"strong"} and by_ticket["easy"] == {"cheap"}
    assert ctl.set_models(hard, worker="default")["models"] == {"manager": "strong"}
    ctl.close()


def test_cross_department_requests_can_wait_for_the_top_managers_approval(store, tmp_path):
    from orchestrator import actions
    asker = top(store)
    box = ToolBox(tmp_path, "research_manager", store=store, ticket_id=asker,
                  departments={"software": "software_manager"}, approve_requests=True)
    r = box.call("request_work", {"department": "software", "title": "build X", "need": "X is missing"})
    assert r["ok"] and "proposed" in r["note"]
    t = store.get_ticket(r["ticket_id"])
    assert t["status"] == "escalated" and t["escalation"]["to"] == "top_manager" and t["escalation"]["kind"] == "proposal"
    assert actions.retry(store, t["id"], "approved", "top_manager")["status"] == "in_progress"     # approve


# ------------------------------------------------------------------------- a model out of quota is skipped, not fatal

def test_a_model_that_hits_its_usage_limit_is_skipped_for_the_rest_of_the_run(store, tmp_path):
    from orchestrator import providers
    from orchestrator.providers import ModelUnavailable
    providers.UNAVAILABLE.clear()

    class Limited(Probe):
        def complete(self, *, model, system, messages, max_tokens=16000):
            if model == "cli-haiku":
                self.calls.append({"model": model, "system": system, "messages": messages})
                raise ModelUnavailable("claude exited with code 1: You've hit your 5-hour limit")
            return super().complete(model=model, system=system, messages=messages, max_tokens=max_tokens)

    p = Limited(delay=0)
    m = Manager("research_manager", store, p, load_config(None), tmp_path, "role", ["cli-haiku", "flash"],
                ["cli-haiku", "flash"])
    try:
        assert m.work(top(store))["status"] == "done"
        assert "cli-haiku" in providers.UNAVAILABLE
        assert sum(1 for c in p.calls if c["model"] == "cli-haiku") <= 2          # tried once (or racing workers), then skipped
        tid = store.create_ticket({**SPEC, "title": "solo", "assignee": "worker:r-9"}, "m")["id"]
        out = run_ticket(store, Limited(delay=0), tid, "w", "role", ["cli-haiku"])
        assert out["status"] == "escalated" and "no usable model" in out["escalation"]["reason"]
    finally:
        providers.UNAVAILABLE.clear()


# ------------------------------------------------------------------------- real money: off-peak prices, balance floor

def test_off_peak_calls_are_priced_at_the_off_peak_rate(store):
    from orchestrator.limits import cost_estimate
    c = Completion(text="x", model="flash", input_tokens=1_000_000, output_tokens=1_000_000, cache_hit_tokens=0)
    store.record_usage(None, "w", "research", c)
    cfg = {"prices": {"flash": {"input": 1.0, "cache_hit": 0.1, "output": 2.0, "off_peak": 0.5}}}
    assert cost_estimate(store, cfg) == pytest.approx(3.0)                       # no window: normal rate
    now = store.last_event_ts()[11:16]                                          # the call's UTC HH:MM
    h, m = int(now[:2]), int(now[3:])
    start = f"{(h - 1) % 24:02d}:{m:02d}"; end = f"{(h + 1) % 24:02d}:{m:02d}"
    cfg["price_off_peak_utc"] = [start, end]                                     # window around now (may wrap)
    assert cost_estimate(store, cfg) == pytest.approx(1.5)
    cfg["price_off_peak_utc"] = [end, start]                                     # the complement: peak again
    assert cost_estimate(store, cfg) == pytest.approx(3.0)


def test_a_real_balance_below_the_floor_halts_model_calls(store, tmp_path):
    cfg = load_config(None)
    cfg["budget"]["stop_below_balance_usd"] = 1.0
    bal = {"deepseek": {"usd": 5.0, "available": True}}
    g = GuardedProvider(Probe(delay=0), store, cfg, tmp_path, balance_fn=lambda: dict(bal))
    cfg["budget"]["balance_check_seconds"] = 0
    g.check()
    bal["deepseek"] = {"usd": 0.4, "available": True}
    with pytest.raises(Halted, match="below the floor"):
        g.check()
    bal["deepseek"] = {"error": "network down"}                                 # an unreadable balance never halts
    g.check()


def test_fetch_balances_reads_deepseek_and_never_returns_the_key(monkeypatch):
    from orchestrator.providers import fetch_balances
    monkeypatch.setenv("DS_KEY", "sk-secret")
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "6.61"}]}

    def get(url, headers):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return R()

    cfg = {"providers": {"deepseek": {"api_key_env": "DS_KEY", "base_url": "https://api.deepseek.com"},
                         "local": {"base_url": "http://localhost:11434/v1"}}}
    out = fetch_balances(cfg, http_get=get)
    assert out == {"deepseek": {"usd": 6.61, "currency": "USD", "available": True}}
    assert seen["url"] == "https://api.deepseek.com/user/balance" and seen["auth"] == "Bearer sk-secret"
    assert "sk-secret" not in json.dumps(out)


def test_cancel_frees_hardware_once_no_job_of_the_ticket_is_running(store, tmp_path):
    import subprocess as sp, sys as _sys
    import psutil
    from orchestrator import actions
    store.register_resource("rig", "safe between runs")
    store.register_resource("pc", "safe between runs")
    idle = store.create_ticket({**SPEC, "title": "idle", "assignee": "worker:r-1"}, "m")["id"]
    busy = store.create_ticket({**SPEC, "title": "busy", "assignee": "worker:r-2"}, "m")["id"]
    store.claim_resource("rig", idle, "1m", "w1")
    store.claim_resource("pc", busy, "1m", "w2")
    proc = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.create_job(busy, "w2", "long", ["x"], str(tmp_path / "log.txt"), resource="pc", pid=proc.pid,
                         job_id="j1", pid_started=psutil.Process(proc.pid).create_time())
        assert actions.cancel(store, idle, "no longer needed", "top_manager")["released"] == ["rig"]
        assert actions.cancel(store, busy, "no longer needed", "top_manager")["released"] == []   # its job still runs
        state = {r["name"]: r["state"] for r in store.list_resources()}
        assert state == {"rig": "free", "pc": "busy"}
    finally:
        proc.kill()
        proc.wait()
    assert actions.release_abandoned_holds(store) == ["pc"]                       # freed as soon as the job ended


def test_a_model_past_its_cost_cap_is_skipped_and_the_fallback_takes_over(store, tmp_path):
    from orchestrator import providers
    providers.UNAVAILABLE.clear()
    cfg = load_config(None)
    cfg["prices"] = {"claude-haiku-5-5": {"input": 1.0, "output": 1.0}, "flash": {"input": 0.0, "output": 0.0}}
    cfg["budget"]["max_cost_usd_by_model"] = {"claude-": 2.0}
    store.record_usage(None, "w", "x", Completion(text="", model="claude-haiku-5-5", input_tokens=3_000_000))
    g = GuardedProvider(Probe(delay=0), store, cfg, tmp_path)
    assert "cost cap for claude-" in (g.model_cap_exhausted("claude-haiku-5-5") or "")
    assert g.model_cap_exhausted("flash") is None
    tid = store.create_ticket({**SPEC, "title": "w", "assignee": "worker:r-1"}, "m")["id"]
    try:
        out = run_ticket(store, g, tid, "w", "role", ["claude-haiku-5-5", "flash"])
        assert out["status"] == "done"
        assert {e["payload"]["model"] for e in store.events(tid) if e["type"] == "usage"} == {"flash"}
    finally:
        providers.UNAVAILABLE.clear()
