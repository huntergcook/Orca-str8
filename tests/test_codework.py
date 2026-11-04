import json
import subprocess

import pytest

from orchestrator import Store
from orchestrator.codework import (CodeError, CodeRepo, CodeSpec, CodeToolBox, glob_match, run_gates)
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.manager import Manager
from orchestrator.providers import MockProvider

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
PYTEST_CMD = "python -m pytest -q tests/test_calc.py"


def j(**kw):
    return json.dumps(kw)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo_dir(tmp_path):
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "calc.py").write_text(BUGGY)
    (r / "tests" / "test_calc.py").write_text(TEST)
    (r / ".gitignore").write_text("workspace/\n__pycache__/\n.env\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def repo(repo_dir, tmp_path):
    return CodeRepo(repo_dir, "main", tmp_path / "worktrees")


SPEC = CodeSpec(["src/calc.py"], [PYTEST_CMD], ["src/calc.py"])


def fix(repo, tid="T1", content="def add(a, b):\n    return a + b\n"):
    wt = repo.ensure_worktree(tid)
    (wt / "src" / "calc.py").write_text(content)
    return wt


# --------------------------------------------------------------------------- globs

@pytest.mark.parametrize("path,pattern,expected", [
    ("plugin/src/capture.cpp", "plugin/src/capture.*", True),
    ("plugin/src/sub/capture.cpp", "plugin/src/capture.*", False),       # '*' stays in one directory
    ("tests/a/b.py", "tests/**", True), ("tests/x.py", "tests/**", True),
    ("a.md", "**/*.md", True), ("x/y/a.md", "**/*.md", True), ("x/y/a.txt", "**/*.md", False),
    ("src/calc.py", "src/calc.py", True), ("src/calc.pyc", "src/calc.py", False),
    ("src/a1.py", "src/a?.py", True), ("src/a12.py", "src/a?.py", False),
    ("tools\\x.py", "tools/*.py", True),
])
def test_glob_match(path, pattern, expected):
    assert glob_match(path, pattern) is expected


def test_spec_problems():
    assert CodeSpec.problems({"allowed_paths": ["a.py"], "acceptance": ["pytest"]}) == []
    assert any("acceptance" in p for p in CodeSpec.problems({"allowed_paths": ["a.py"]}))
    assert any("allowed_paths is empty" in p for p in CodeSpec.problems({"acceptance": ["x"]}))
    assert any("too broad" in p for p in CodeSpec.problems({"allowed_paths": ["**"], "acceptance": ["x"]}))
    assert CodeSpec.problems("nonsense")


# -------------------------------------------------------------------- worktree + merge

def test_worktree_commit_diff_and_merge(repo, repo_dir):
    wt = fix(repo)
    assert wt.exists() and repo.has_worktree("T1")
    sha = repo.commit_all("T1", "T1: fix add")
    assert sha and repo.commit_all("T1", "nothing new") is None                 # no-op when nothing changed
    assert repo.changed_files("T1") == [("M", "src/calc.py")]
    assert "+    return a + b" in repo.diff("T1") and "calc.py" in repo.diffstat("T1")
    merge_sha = repo.merge("T1", "Merge T1")
    assert "a + b" in (repo_dir / "src" / "calc.py").read_text()                # main now has the fix
    assert not repo.has_worktree("T1") and not git(repo_dir, "branch", "--list", "ticket/T1").strip()   # cleaned up
    assert git(repo_dir, "log", "-1", "--format=%h").strip() == merge_sha


def test_merge_refuses_a_dirty_repo_or_wrong_branch_and_aborts_cleanly_on_conflict(repo, repo_dir):
    fix(repo)
    repo.commit_all("T1", "T1")
    (repo_dir / "src" / "calc.py").write_text("changed in main, uncommitted\n")
    with pytest.raises(CodeError, match="uncommitted changes"):
        repo.merge("T1", "m")
    git(repo_dir, "checkout", "-q", "--", "src/calc.py")
    git(repo_dir, "checkout", "-q", "-b", "other")
    with pytest.raises(CodeError, match="not 'main'"):
        repo.merge("T1", "m")
    git(repo_dir, "checkout", "-q", "main")
    # main changes the same line the branch changed -> conflict
    (repo_dir / "src" / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    git(repo_dir, "commit", "-q", "-am", "main edits the same line")
    with pytest.raises(CodeError, match="merge conflict"):
        repo.merge("T1", "m")
    assert git(repo_dir, "status", "--porcelain").strip() == ""                  # aborted: tree is clean again
    assert "a * b" in (repo_dir / "src" / "calc.py").read_text()
    assert repo.has_worktree("T1")                                                # the branch is kept for rework


def test_discard_removes_worktree_and_branch(repo, repo_dir):
    fix(repo)
    repo.discard("T1")
    assert not repo.has_worktree("T1") and not git(repo_dir, "branch", "--list", "ticket/T1").strip()
    repo.discard("T1")                                                            # idempotent
    assert repo.ensure_worktree("T1").exists()                                    # and can be recreated fresh


def test_two_tickets_get_independent_worktrees(repo):
    a, b = repo.ensure_worktree("A"), repo.ensure_worktree("B")
    (a / "src" / "calc.py").write_text("A\n")
    assert (b / "src" / "calc.py").read_text() == BUGGY
    assert repo.ensure_worktree("A") == a                                         # reuse on retry


# ------------------------------------------------------------------------------ gates

def gate(gates, name):
    return next(g for g in gates if g["name"].startswith(name))


def test_gates_pass_for_a_good_change_and_fail_for_the_unfixed_test(repo):
    repo.ensure_worktree("T1")
    (repo.worktree_path("T1") / "src" / "calc.py").write_text(BUGGY + "# touched\n")
    repo.commit_all("T1", "T1")
    gates = run_gates(repo, "T1", SPEC, {})
    assert gate(gates, "path policy")["ok"] and gate(gates, "secret scan")["ok"]
    assert not gate(gates, "acceptance")["ok"] and "assert" in gate(gates, "acceptance")["detail"]   # the bug is still there
    fix(repo, "T2")
    repo.commit_all("T2", "T2")
    assert all(g["ok"] for g in run_gates(repo, "T2", SPEC, {}))


def test_gate_catches_writes_outside_allowed_paths_and_empty_changes(repo):
    wt = fix(repo)
    (wt / "README.md").write_text("sneaky\n")
    repo.commit_all("T1", "T1")
    g = gate(run_gates(repo, "T1", SPEC, {}), "path policy")
    assert not g["ok"] and "README.md" in g["detail"]
    repo.ensure_worktree("EMPTY")
    assert "no files were changed" in gate(run_gates(repo, "EMPTY", SPEC, {}), "path policy")["detail"]


def test_secret_scan_catches_keys_private_keys_and_env_files(repo):
    spec = CodeSpec(["src/*", ".env", "src/.env.*"], ["python -c \"pass\""])
    for name, body in {"a": 'API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n', "b": "-----BEGIN RSA PRIVATE KEY-----\n",
                       "c": "token = sk-" + "a1B2c3D4" * 4 + "\n", "d": "x = 1\n"}.items():
        wt = repo.ensure_worktree(name)
        (wt / "src" / f"{name}.py").write_text(body)
        repo.commit_all(name, name)
        ok = gate(run_gates(repo, name, spec, {}), "secret scan")["ok"]
        assert ok is (name == "d"), (name, ok)
    wt = repo.ensure_worktree("e")
    (wt / ".env").write_text("X=1\n")
    repo.commit_all("e", "e")                                                    # .env is gitignored so it is not even committed
    (wt / "src" / ".env.local").write_text("X=1\n")
    repo.commit_all("e", "e2")
    assert not gate(run_gates(repo, "e", spec, {}), "secret scan")["ok"]
    assert gate(run_gates(repo, "e", spec, {"gates": {"secret_scan": False}}), "path policy")                # can be disabled


def test_extra_command_gates_and_timeouts(repo):
    fix(repo)
    repo.commit_all("T1", "T1")
    gates = run_gates(repo, "T1", SPEC, {"gates": {"extra_commands": ["python -c \"import sys; sys.exit(3)\""]}})
    assert not gate(gates, "extra")["ok"] and "exit 3" in gate(gates, "extra")["detail"]
    slow = run_gates(repo, "T1", CodeSpec(["src/calc.py"], ["python -c \"import time; time.sleep(5)\""]), {"test_timeout": 0.5})
    assert "timed out" in gate(slow, "acceptance")["detail"]


# ------------------------------------------------------------------------------ tools

@pytest.fixture
def box(repo, tmp_path):
    wt = repo.ensure_worktree("T1")
    (wt / ".env").write_text("SECRET=1\n")
    return CodeToolBox(tmp_path / "ws", "worker:code_worker-1", root=wt, spec=SPEC)


def test_the_code_worker_can_only_use_the_code_tools(box):
    for tool in ("run_python", "pip_install", "claim_resource", "start_job", "acquire_git"):
        r = box.call(tool, {})
        assert not r["ok"] and "not available for code tickets" in r["error"]
    assert "edit_file" in box.describe() and "src/calc.py" in box.describe() and PYTEST_CMD in box.describe()


def test_reads_listing_and_search(box):
    assert "return a - b" in box.call("read_file", {"path": "src/calc.py"})["content"]
    ranged = box.call("read_file", {"path": "src/calc.py", "start_line": 2, "end_line": 2})
    assert ranged["content"] == "2:     return a - b" and ranged["lines"] == "2-2 of 2"
    names = {e["name"] for e in box.call("list_dir", {"path": "."})["entries"]}
    assert {"src/", "tests/"} <= names and ".git" not in names and ".git/" not in names and ".env" not in names
    hits = box.call("search_text", {"pattern": r"def \w+\(", "glob": "**/*.py"})["matches"]
    assert any(h.startswith("src/calc.py:1:") for h in hits) and any("tests/test_calc.py" in h for h in hits)
    assert not box.call("search_text", {"pattern": "("})["ok"]                                  # bad regex is an error, not a crash
    assert not any(".env" in h for h in box.call("search_text", {"pattern": "SECRET"})["matches"])


def test_secrets_traversal_and_git_are_off_limits(box):
    assert "secrets file" in box.call("read_file", {"path": ".env"})["error"]
    for bad in ("../outside.txt", "../../etc/passwd", ".git/config"):
        r = box.call("read_file", {"path": bad})
        assert not r["ok"] and ("outside the repository" in r["error"] or ".git" in r["error"])
    assert not box.call("write_file", {"path": ".git/hooks/pre-commit", "content": "x"})["ok"]
    assert not box.call("write_file", {"path": "../escape.py", "content": "x"})["ok"]


def test_writes_are_limited_to_allowed_paths(box, repo):
    ok = box.call("write_file", {"path": "src/calc.py", "content": "def add(a, b):\r\n    return a + b\r\n"})
    assert ok["ok"] and (repo.worktree_path("T1") / "src" / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"   # LF kept
    denied = box.call("write_file", {"path": "tests/test_calc.py", "content": "assert True"})
    assert not denied["ok"] and "not in this ticket's allowed_paths" in denied["error"]
    wide = CodeToolBox(box.workspace, "w", root=box.root, spec=CodeSpec(["src/*"], ["true"]))
    assert "secrets file" in wide.call("write_file", {"path": "src/.env.prod", "content": "x"})["error"]
    assert "200KB" in box.call("write_file", {"path": "src/calc.py", "content": "x" * 300_000})["error"]


def test_edit_file_needs_one_exact_match(box, repo):
    assert box.call("edit_file", {"path": "src/calc.py", "old": "a - b", "new": "a + b"})["replaced"] == 1
    assert "a + b" in (repo.worktree_path("T1") / "src" / "calc.py").read_text()
    assert "not found" in box.call("edit_file", {"path": "src/calc.py", "old": "nope", "new": "x"})["error"]
    box.call("write_file", {"path": "src/calc.py", "content": "x = 1\nx = 1\n"})
    assert "matches 2 places" in box.call("edit_file", {"path": "src/calc.py", "old": "x = 1", "new": "x = 2"})["error"]
    wide = CodeToolBox(box.workspace, "w", root=box.root, spec=CodeSpec(["src/*.py"], ["true"]))
    assert "does not exist" in wide.call("edit_file", {"path": "src/missing.py", "old": "a", "new": "b"})["error"]
    assert "not in this ticket's allowed_paths" in box.call("edit_file", {"path": "src/missing.py", "old": "a", "new": "b"})["error"]
    assert not box.call("edit_file", {"path": "tests/test_calc.py", "old": "add", "new": "sub"})["ok"]    # not allowed
    assert "must not be empty" in box.call("edit_file", {"path": "src/calc.py", "old": "", "new": "x"})["error"]


def test_run_tests_runs_the_ticket_acceptance_commands(box):
    first = box.call("run_tests", {})
    assert first["all_passed"] is False and "assert" in first["results"][0]["detail"]
    box.call("edit_file", {"path": "src/calc.py", "old": "a - b", "new": "a + b"})
    assert box.call("run_tests", {})["all_passed"] is True


# --------------------------------------------------- the whole flow through a manager

def cfg_for(repo_dir, tmp_path):
    cfg = load_config(None)
    cfg["code"].update(repo=str(repo_dir), base_branch="main", worktrees_dir=str(tmp_path / "worktrees"))
    cfg["departments"] = {"code": {}}
    return cfg


WORKER_TICKET = dict(
    title="Fix add", worker_role="code_worker", context="add() subtracts; make it add.",
    acceptance_criteria=["add(2, 3) == 5"], return_spec={"format": "summary", "description": "what changed"},
    code={"allowed_paths": ["src/calc.py"], "acceptance": [PYTEST_CMD], "context_files": ["src/calc.py"]})

EDIT = j(action="tool", tool="edit_file", args={"path": "src/calc.py", "old": "a - b", "new": "a + b"})
DONE = j(action="done", result={"summary": "fixed the sign", "open_questions": []})


def top_ticket(store, tickets):
    return store.create_ticket({"title": "Fix the calculator", "assignee": "code_manager", "worker_role": "worker",
                                "context": "c", "acceptance_criteria": ["a"],
                                "return_spec": {"format": "f", "description": "d"}, "worker_tickets": tickets},
                               "top_manager")["id"]


def test_a_code_ticket_runs_in_a_worktree_passes_its_gates_and_waits_for_review(repo_dir, tmp_path):
    cfg = cfg_for(repo_dir, tmp_path)
    store = Store(tmp_path / "t.db")
    p = MockProvider([EDIT, DONE, j(action="done", result={"report": "1 branch ready for review"})])
    m = Manager("code_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"])
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done"
    kid = store.list_tickets(parent_id=top)[0]
    assert kid["assignee"] == "worker:code_worker-1" and kid["status"] == "done"
    assert kid["result"]["code"]["branch"] == f"ticket/{kid['id']}" and kid["result"]["code"]["files"] == ["src/calc.py"]
    assert "a - b" in (repo_dir / "src" / "calc.py").read_text()                 # NOT merged: main is untouched
    types = [e["type"] for e in store.events(kid["id"])]
    assert "code_started" in types and "code_gates" in types and types[-2:] == ["code_ready", "status_changed"]
    assert "Code ticket" in p.calls[0]["messages"][0]["content"] and "src/calc.py" in p.calls[0]["messages"][0]["content"]
    assert "def add" in p.calls[0]["messages"][0]["content"]                     # the context file was shown up front
    assert "Code Worker" in p.calls[0]["system"] and "edit_file" in p.calls[0]["system"]
    assert len(p.calls) == 3                                                      # worker x2, manager x1: no planning call
    store.close()


def test_gate_failures_go_back_to_the_same_conversation_until_fixed(repo_dir, tmp_path):
    cfg = cfg_for(repo_dir, tmp_path)
    store = Store(tmp_path / "t.db")
    p = MockProvider([DONE,                                                       # says done without changing anything
                      EDIT, DONE,                                                 # reads the feedback, fixes, says done
                      j(action="done", result={})])
    m = Manager("code_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"])
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done"
    kid = store.list_tickets(parent_id=top)[0]
    assert kid["status"] == "done"
    rejected = [e for e in store.events(kid["id"]) if e["type"] == "done_rejected"]
    assert len(rejected) == 1 and "no files were changed" in rejected[0]["payload"]["feedback"]
    feedback_turn = p.calls[1]["messages"][-1]["content"]
    assert "These checks FAILED" in feedback_turn and "path policy" in feedback_turn
    assert [g["ok"] for g in [e for e in store.events(kid["id"]) if e["type"] == "code_gates"][-1]["payload"]["gates"]] == [True, True, True]
    store.close()


def test_a_worker_that_never_passes_escalates_and_its_work_is_kept(repo_dir, tmp_path):
    cfg = cfg_for(repo_dir, tmp_path)
    store = Store(tmp_path / "t.db")
    wrong = j(action="tool", tool="edit_file", args={"path": "src/calc.py", "old": "a - b", "new": "a * b"})
    p = MockProvider([wrong, DONE, DONE, DONE, j(action="escalate", reason="worker could not pass")])
    m = Manager("code_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"])
    top = top_ticket(store, [WORKER_TICKET])
    m.work(top)
    kid = store.list_tickets(parent_id=top)[0]
    assert kid["status"] == "escalated" and "still failing its checks after 3 attempts" in kid["escalation"]["reason"]
    assert kid["escalation"]["to"] == "manager" and kid["escalation"]["kind"] == "technical"
    assert (tmp_path / "worktrees" / kid["id"]).exists()                          # the attempt is kept for inspection or retry
    store.close()


def test_bad_code_specs_are_rejected_before_anything_is_created(repo_dir, tmp_path):
    cfg = cfg_for(repo_dir, tmp_path)
    store = Store(tmp_path / "t.db")
    m = Manager("code_manager", store, MockProvider([]), cfg, tmp_path / "ws", "role", ["m"], ["w"])
    top = top_ticket(store, [])
    good = dict(WORKER_TICKET)
    with pytest.raises(Exception, match="acceptance is empty"):
        m._delegate(top, {"tickets": [good, {**good, "title": "bad", "code": {"allowed_paths": ["src/x.py"]}}]})
    assert store.list_tickets(parent_id=top) == []                               # even the good one was not created
    cfg["code"]["repo"] = str(tmp_path)                                           # not a git repo
    m2 = Manager("code_manager", store, MockProvider([]), cfg, tmp_path / "ws", "role", ["m"], ["w"])
    with pytest.raises(Exception, match="need a git repository"):
        m2._delegate(top, {"tickets": [good]})
    store.close()


# ------------------------------------------------------------- review with ctl code

def make_ctl(repo_dir, tmp_path, script):
    cfg = cfg_for(repo_dir, tmp_path)
    cfg["workspace"] = str(tmp_path / "ws")
    return Ctl(cfg, tmp_path / "ws", MockProvider(script))


def create(ctl, tickets=None):
    return ctl.create(dict(department="code", title="Fix the calculator", context="c", acceptance_criteria=["a"],
                           return_spec={"format": "f", "description": "d"},
                           worker_tickets=tickets or [WORKER_TICKET]))["ticket_id"]


def test_review_accept_merges_the_branch_into_main(repo_dir, tmp_path):
    ctl = make_ctl(repo_dir, tmp_path, [EDIT, DONE, j(action="done", result={"report": "ready"})])
    create(ctl)
    ctl.run()
    pending = ctl.code_list()["awaiting_review"]
    assert len(pending) == 1 and pending[0]["gates_passed"] and pending[0]["files"] == ["src/calc.py"]
    tid = pending[0]["id"]
    packet = ctl.code_show(tid)
    assert packet["summary"] == "fixed the sign" and "+    return a + b" in packet["diff"]
    assert packet["files"] == ["M src/calc.py"] and all(g["ok"] for g in packet["gates"])
    out = ctl.code_accept(tid)
    assert out["accepted"] == tid and "a + b" in (repo_dir / "src" / "calc.py").read_text()
    assert ctl.code_list()["awaiting_review"] == []
    assert "merge" in git(repo_dir, "log", "-1", "--format=%s").lower()
    with pytest.raises(Exception, match="not awaiting code review"):
        ctl.code_accept(tid)                                                      # cannot accept twice
    ctl.close()


def test_review_reject_files_a_replacement_ticket_with_the_feedback(repo_dir, tmp_path):
    ctl = make_ctl(repo_dir, tmp_path, [EDIT, DONE, j(action="done", result={}),
                                        EDIT, DONE, j(action="done", result={})])    # second round, after the redo
    create(ctl)
    ctl.run()
    tid = ctl.code_list()["awaiting_review"][0]["id"]
    with pytest.raises(Exception, match="needs feedback"):
        ctl.code_reject(tid, "  ")
    out = ctl.code_reject(tid, "Also handle floats; add a test for 0.1 + 0.2.")
    assert out["rejected"] == tid and out["assignee"] == "code_manager"
    assert ctl.code_list()["awaiting_review"] == [] and not (tmp_path / "worktrees" / tid).exists()
    assert "a - b" in (repo_dir / "src" / "calc.py").read_text()                 # nothing merged
    redo = ctl.store.get_ticket(out["replacement_ticket"])
    assert redo["title"].startswith("Redo:") and "Also handle floats" in redo["context"]
    assert redo["worker_tickets"][0]["code"]["allowed_paths"] == ["src/calc.py"]
    assert "Also handle floats" in redo["worker_tickets"][0]["context"]           # the worker will see it
    ctl.run()                                                                     # the replacement runs and awaits review again
    again = ctl.code_list()["awaiting_review"]
    assert len(again) == 1 and again[0]["id"] != tid
    ctl.close()


def test_review_discard_and_unknown_tickets(repo_dir, tmp_path):
    ctl = make_ctl(repo_dir, tmp_path, [EDIT, DONE, j(action="done", result={})])
    create(ctl)
    ctl.run()
    tid = ctl.code_list()["awaiting_review"][0]["id"]
    assert ctl.code_discard(tid) == {"discarded": tid}
    assert ctl.code_list()["awaiting_review"] == []
    with pytest.raises(Exception, match="not awaiting code review"):
        ctl.code_show(tid)                                                        # already discarded
    with pytest.raises(Exception, match="nope-123"):
        ctl.code_show("nope-123")                                                 # no such ticket
    ctl.close()


def test_the_dashboard_lists_code_awaiting_review(repo_dir, tmp_path):
    from orchestrator.dashboard import build_state
    ctl = make_ctl(repo_dir, tmp_path, [EDIT, DONE, j(action="done", result={})])
    create(ctl)
    ctl.run()
    s = build_state(tmp_path / "ws" / "orchestrator.db", ctl.cfg)
    assert [c["files"] for c in s["code_pending"]] == [["src/calc.py"]]
    ctl.code_accept(ctl.code_list()["awaiting_review"][0]["id"])
    assert build_state(tmp_path / "ws" / "orchestrator.db", ctl.cfg)["code_pending"] == []
    ctl.close()


# ----------------------------------------------------- the Software department as a whole

def test_verifiers_review_the_actual_patch_and_the_rules_apply_to_it(repo_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "RULES.md").write_text("1. No decompiled code in the repo.", encoding="utf-8")
    cfg = cfg_for(repo_dir, tmp_path)
    cfg["rules_file"] = "RULES.md"
    cfg["verification"]["enabled"] = True
    store = Store(tmp_path / "t.db")
    from orchestrator.verification import Verifier
    p = MockProvider([EDIT, DONE, j(action="done", result={"report": "fixed"}), j(verdict="pass", issues=[], advice="")])
    v = Verifier(store, p, cfg, tmp_path / "ws", ["verifier-model"])
    m = Manager("software_manager", store, p, cfg, tmp_path / "ws", "role", ["m"], ["w"], verifier=v)
    top = store.create_ticket({"title": "Fix", "assignee": "software_manager", "worker_role": "worker", "context": "c",
                               "acceptance_criteria": ["a"], "return_spec": {"format": "f", "description": "d"},
                               "worker_tickets": [WORKER_TICKET]}, "top_manager")["id"]
    assert m.work(top)["status"] == "done"
    seen = p.calls[-1]["messages"][0]["content"]                                  # what the verifier was shown
    assert "PATCH for" in seen and "+    return a + b" in seen and "-    return a - b" in seen
    assert "No decompiled code in the repo" in seen and "automatic FAIL" in seen
    store.close()


def test_the_scaffold_includes_a_software_department(tmp_path):
    from orchestrator.init import scaffold
    scaffold(tmp_path / "proj")
    cfg = load_config(tmp_path / "proj" / "config" / "orchestrator.yaml")
    assert cfg["departments"]["software"]["manager_role"] == "roles/software_manager.md"
    from pathlib import Path
    assert "Software Manager" in (Path(__file__).resolve().parents[1] / "roles" / "software_manager.md").read_text(encoding="utf-8")
    assert (Path(__file__).resolve().parents[1] / "roles" / "workers" / "code_worker.md").exists()
