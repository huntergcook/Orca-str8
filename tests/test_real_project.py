"""Things a real, existing project exposes: its own package called 'orchestrator', and a repo workers must read."""
import json
import subprocess
import sys

import pytest

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.init import command_text
from orchestrator.manager import Manager
from orchestrator.providers import MockProvider
from orchestrator.tools import ToolBox


def j(**kw):
    return json.dumps(kw)


# ------------------------------------------------------------ the name collision

def make_project_with_its_own_orchestrator_package(tmp_path):
    proj = tmp_path / "proj"
    (proj / "orchestrator").mkdir(parents=True)                       # the project's OWN package
    (proj / "orchestrator" / "__init__.py").write_text("# the project's dispatcher, not ours\n", encoding="utf-8")
    # the real fnv-vr project has a config.py too, and that name exists in our package as well
    (proj / "orchestrator" / "config.py").write_text("PROJECT_SETTINGS = {}\n", encoding="utf-8")
    (proj / "config").mkdir()
    (proj / "config" / "orchestrator.yaml").write_text("departments:\n  research: {}\n", encoding="utf-8")
    return proj


def test_without_isolation_the_projects_package_shadows_ours(tmp_path):
    """Documents the bug seen in a real project: our ctl is found, but its `from .config import ...` resolves
    to the PROJECT's config.py and fails."""
    proj = make_project_with_its_own_orchestrator_package(tmp_path)
    r = subprocess.run([sys.executable, "-m", "orchestrator.ctl", "--project", str(proj), "resume"],
                       cwd=proj, capture_output=True, text=True)
    assert r.returncode != 0 and "cannot import name" in r.stderr


def test_with_p_our_package_is_used_even_inside_such_a_project(tmp_path):
    proj = make_project_with_its_own_orchestrator_package(tmp_path)
    r = subprocess.run([sys.executable, "-P", "-m", "orchestrator.ctl", "--project", str(proj), "resume"],
                       cwd=proj, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["departments"] == ["research"]
    assert (proj / "workspace" / "orchestrator.db").exists()          # state landed in the project's workspace


def test_every_generated_command_isolates_the_working_directory(tmp_path):
    text = command_text(tmp_path)
    assert "python -P -m orchestrator.ctl" in text and "python -P -m orchestrator.smoke" in text
    assert "python -m orchestrator." not in text


def test_the_run_launched_from_the_dashboard_is_isolated_too():
    import inspect
    from orchestrator import actions
    assert '"-P"' in inspect.getsource(actions.spawn_run)


# ------------------------------------------------------------- read-only repo access

@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "vr_session.h").write_text("// stereo setup\nint eyes = 2;", encoding="utf-8")
    (r / "PLAN.md").write_text("# Plan", encoding="utf-8")
    (r / ".env").write_text("DEEPSEEK_API_KEY=sk-should-never-be-read", encoding="utf-8")
    (r / ".env.example").write_text("DEEPSEEK_API_KEY=", encoding="utf-8")
    (r / "id_rsa").write_text("private", encoding="utf-8")
    (r / ".git").mkdir()
    (r / ".git" / "config").write_text("[core]", encoding="utf-8")
    return r


def box(tmp_path, repo):
    return ToolBox(tmp_path / "ws", "w", read_roots=[str(repo)])


def test_workers_can_read_and_list_the_repo_but_not_leave_it(tmp_path, repo):
    b = box(tmp_path, repo)
    assert "int eyes" in b.call("read_file", {"path": str(repo / "src" / "vr_session.h")})["content"]
    listing = b.call("list_dir", {"path": str(repo)})["entries"]
    names = {e["name"] for e in listing}
    assert {"src/", "PLAN.md", ".env.example"} <= names
    assert ".env" not in names and "id_rsa" not in names and ".git/" not in names     # secrets and .git hidden
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    assert "outside the workspace and the configured read roots" in b.call("read_file", {"path": str(outside)})["error"]
    assert "outside" in b.call("list_dir", {"path": str(tmp_path)})["error"]
    assert "outside" in b.call("read_file", {"path": str(repo / ".." / "elsewhere.txt")})["error"]   # traversal


def test_secrets_in_the_repo_are_never_readable(tmp_path, repo):
    b = box(tmp_path, repo)
    for name in (".env", "id_rsa"):
        r = b.call("read_file", {"path": str(repo / name)})
        assert not r["ok"] and "secrets file" in r["error"]
    assert b.call("read_file", {"path": str(repo / ".env.example")})["ok"]            # the template is fine


def test_reading_the_repo_does_not_grant_write_access(tmp_path, repo):
    b = box(tmp_path, repo)
    r = b.call("write_file", {"path": str(repo / "src" / "evil.h"), "content": "x"})
    assert not r["ok"] and "escapes workspace" in r["error"]
    assert not (repo / "src" / "evil.h").exists()
    assert b.call("write_file", {"path": "findings/a.md", "content": "ok"})["ok"]     # workspace writes still fine


def test_relative_paths_still_mean_the_workspace_and_no_roots_means_no_extra_access(tmp_path, repo):
    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "a.txt").write_text("mine", encoding="utf-8")
    plain = ToolBox(ws, "w")                                                           # no read roots configured
    assert plain.call("read_file", {"path": "notes/a.txt"})["content"] == "mine"
    assert not plain.call("read_file", {"path": str(repo / "PLAN.md")})["ok"]
    assert "list_dir" in plain.describe() and "Read-only project folders" not in plain.describe()
    assert "Read-only project folders" in box(tmp_path, repo).describe()


def test_a_worker_reads_the_repo_through_the_manager_config(tmp_path, repo):
    cfg = load_config(None)
    cfg["read_roots"] = [str(repo)]
    store = Store(tmp_path / "t.db")
    spec = dict(title="Read the plan", worker_role="r", context="c", acceptance_criteria=["a"],
                return_spec={"format": "f", "description": "d"})
    p = MockProvider([j(action="delegate", tickets=[spec]),
                      j(action="tool", tool="read_file", args={"path": str(repo / "PLAN.md")}),
                      j(action="done", result={"read": "yes"}),
                      j(action="done", result={"final": 1})])
    m = Manager("research_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"])
    top = store.create_ticket({**spec, "assignee": "research_manager", "worker_role": "worker"}, "top_manager")["id"]
    assert m.work(top)["status"] == "done"
    assert "# Plan" in p.calls[2]["messages"][-1]["content"]            # the file's text reached the worker
    assert str(repo) in p.calls[1]["system"]                             # and it was told which roots it may read
    store.close()


# ------------------------------------------------------------------ mandatory project rules

def test_project_rules_reach_managers_workers_and_verifiers(tmp_path, monkeypatch):
    from orchestrator.verification import Verifier
    monkeypatch.chdir(tmp_path)
    (tmp_path / "RULES.md").write_text("1. NEVER copy decompiled code into the repo.\n2. Secrets stay in .env.", encoding="utf-8")
    cfg = load_config(None)
    cfg["rules_file"] = "RULES.md"
    cfg["verification"]["enabled"] = True
    store = Store(tmp_path / "t.db")
    spec = dict(title="W", worker_role="r", context="c", acceptance_criteria=["a"],
                return_spec={"format": "f", "description": "d"})
    p = MockProvider([j(action="delegate", tickets=[spec]), j(action="done", result={"w": 1}),
                      j(action="done", result={"final": 1}),
                      j(verdict="pass", issues=[], advice="")])
    v = Verifier(store, p, cfg, tmp_path / "ws", ["verifier-model"])
    m = Manager("research_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"], verifier=v)
    top = store.create_ticket({**spec, "assignee": "research_manager", "worker_role": "worker"}, "top_manager")["id"]
    assert m.work(top)["status"] == "done"
    manager_system, worker_system, _, verifier_call = p.calls[0], p.calls[1], p.calls[2], p.calls[3]
    assert "NEVER copy decompiled code" in manager_system["system"] and "apply them to every worker ticket" in manager_system["system"]
    assert "NEVER copy decompiled code" in worker_system["system"] and "breaking one makes the work unacceptable" in worker_system["system"]
    assert "NEVER copy decompiled code" in verifier_call["messages"][0]["content"] and "automatic FAIL" in verifier_call["messages"][0]["content"]
    store.close()


def test_no_rules_file_changes_nothing_and_a_missing_one_is_harmless(tmp_path, monkeypatch):
    from orchestrator.config import project_rules
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    assert cfg["rules_file"] is None and project_rules(cfg) == ""
    cfg["rules_file"] = "DOES_NOT_EXIST.md"
    assert project_rules(cfg) == ""
    (tmp_path / "R.md").write_text("x" * 20000, encoding="utf-8")
    cfg["rules_file"] = "R.md"
    assert len(project_rules(cfg)) == 8000                                   # capped so it cannot swamp a prompt
    cfg["rules_file"] = str(tmp_path / "R.md")                               # absolute paths work too
    assert len(project_rules(cfg)) == 8000
