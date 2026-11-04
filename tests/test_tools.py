import json
import subprocess

import pytest

from orchestrator import Store
from orchestrator.agent import run_ticket
from orchestrator.providers import MockProvider
from orchestrator.tools import ToolBox, ToolRegistry


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def j(**kw):
    return json.dumps(kw)


def make_repo(path, with_license=True):
    path.mkdir()
    (path / "README.md").write_text("tool")
    if with_license:
        (path / "LICENSE").write_text("MIT License\nCopyright ...")
    env = ["-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), *env, "commit", "-qm", "init"], check=True)
    return str(path)


def test_registry_register_and_search(tmp_path):
    r = ToolRegistry(tmp_path)
    r.register({"name": "ghidra", "description": "reverse engineering suite", "kind": "vendored", "tags": ["binary"]})
    r.register({"name": "sweeper", "description": "run parameter sweeps on hardware", "kind": "written"})
    assert r.search("reverse engineer binary")[0]["name"] == "ghidra"
    assert r.search("nothing matches this zzz") == []
    with pytest.raises(Exception):
        r.register({"name": "../evil", "description": "x", "kind": "written"})


def test_acquisition_restricted_to_tooling(tmp_path):
    box = ToolBox(tmp_path, "worker:x", can_acquire=False)
    for tool, args in (("search_github", {"query": "q"}), ("acquire_git", {"url": "https://x/y", "name": "n"}),
                       ("register_tool", {"name": "n", "description": "d", "kind": "written"})):
        res = box.call(tool, args)
        assert not res["ok"] and "restricted" in res["error"]
    assert box.call("search_registry", {"query": "x"})["ok"]


def test_search_github_uses_injected_http(tmp_path):
    seen = {}

    def fake(url):
        seen["url"] = url
        return {"items": [{"full_name": "a/b", "description": "d", "stargazers_count": 5,
                           "license": {"spdx_id": "MIT"}, "html_url": "https://github.com/a/b", "pushed_at": "x"}]}

    box = ToolBox(tmp_path, "tool_scout", can_acquire=True, http_get=fake)
    res = box.call("search_github", {"query": "reverse engineering"})
    assert res["results"][0]["license"] == "MIT" and "reverse%20engineering" in seen["url"]


def test_acquire_git_clones_and_registers(tmp_path):
    src = make_repo(tmp_path / "src")
    box = ToolBox(tmp_path / "ws", "tool_scout", can_acquire=True, allow_local_git=True)
    res = box.call("acquire_git", {"url": src, "name": "mytool", "description": "does things", "tags": ["x"]})
    assert res["ok"], res
    entry = ToolRegistry(tmp_path / "ws").get("mytool")
    assert entry["kind"] == "vendored" and entry["license"].startswith("MIT") and entry["added_by"] == "tool_scout"
    assert (tmp_path / "ws" / "tools" / "vendor" / "mytool" / "README.md").exists()
    again = box.call("acquire_git", {"url": src, "name": "mytool"})
    assert not again["ok"] and "already" in again["error"]


def test_acquire_rejects_bad_inputs(tmp_path):
    box = ToolBox(tmp_path, "tool_scout", can_acquire=True)
    assert "https" in box.call("acquire_git", {"url": "file:///etc", "name": "n"})["error"]
    assert "invalid" in box.call("acquire_git", {"url": "https://github.com/a/b", "name": "../x"})["error"]


def test_missing_license_warns(tmp_path):
    src = make_repo(tmp_path / "src", with_license=False)
    box = ToolBox(tmp_path / "ws", "tool_scout", can_acquire=True, allow_local_git=True)
    res = box.call("acquire_git", {"url": src, "name": "nolic"})
    assert res["warning"] and ToolRegistry(tmp_path / "ws").get("nolic")["license"] == "NO LICENSE FILE FOUND"


def test_request_tool_files_ticket_to_tooling_with_top_cc(store, tmp_path):
    ToolRegistry(tmp_path).register({"name": "plotter", "description": "plot sweep data", "kind": "written"})
    box = ToolBox(tmp_path, "worker:exp", can_acquire=False, store=store)
    res = box.call("request_tool", {"title": "need plotter for sweep data", "need": "plot sweep results"})
    t = store.get_ticket(res["ticket_id"])
    assert t["assignee"] == "tooling_manager" and t["cc"] == ["top_manager"] and t["created_by"] == "worker:exp"
    assert res["possibly_relevant_existing"][0]["name"] == "plotter"


def test_worker_loop_tool_calls_do_not_consume_retries(store, tmp_path):
    tid = store.create_ticket(dict(
        title="T", assignee="w", worker_role="tool_scout", context="c", acceptance_criteria=["a"],
        return_spec={"format": "f", "description": "d"}), actor="m")["id"]
    ToolRegistry(tmp_path).register({"name": "ghidra", "description": "reverse engineering", "kind": "vendored"})
    p = MockProvider([
        j(action="tool", tool="search_registry", args={"query": "reverse engineering"}),
        j(action="tool", tool="search_registry", args={"query": "disassembler"}),
        j(action="tool", tool="search_registry", args={"query": "binary"}),
        j(action="tool", tool="search_registry", args={"query": "x"}),
        j(action="done", result={"use": "ghidra"}),
    ])
    box = ToolBox(tmp_path, "w", store=store)
    t = run_ticket(store, p, tid, "w", "role", ["m"], capability_retry_limit=1, toolbox=box)
    assert t["status"] == "done"
    assert "ghidra" in p.calls[1]["messages"][-1]["content"]
    assert "search_registry" in p.calls[0]["system"] and "search_github" not in p.calls[0]["system"]
    assert sum(e["type"] == "tool_call" for e in store.events(tid)) == 4


def test_tool_step_cap(store, tmp_path):
    tid = store.create_ticket(dict(
        title="T", assignee="w", worker_role="r", context="c", acceptance_criteria=["a"],
        return_spec={"format": "f", "description": "d"}), actor="m")["id"]
    loop = [j(action="tool", tool="search_registry", args={"query": "x"})] * 10
    t = run_ticket(store, MockProvider(loop), tid, "w", "r", ["m"], toolbox=ToolBox(tmp_path, "w"),
                   max_tool_steps=3)
    assert t["status"] == "escalated"
