import json
import re
import subprocess
import threading
import time

import pytest

from orchestrator import Store
from orchestrator.codework import CodeError, CodeRepo, CodeSpec, CodeToolBox, branch_state, glob_match, run_gates
from orchestrator.config import load_config
from orchestrator.ctl import Ctl
from orchestrator.manager import Manager
from orchestrator.providers import Completion

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
    (r / "src" / "other.py").write_text("X = 1\n")
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
FIXED = "def add(a, b):\n    return a + b\n"


def fix(repo, tid="T1", content=FIXED, path="src/calc.py"):
    wt = repo.ensure_worktree(tid)
    (wt / path).write_text(content)
    return wt


def show(repo_dir, ref_path):
    return git(repo_dir, "show", ref_path)


# --------------------------------------------------------------------------- globs

@pytest.mark.parametrize("path,pattern,expected", [
    ("plugin/src/capture.cpp", "plugin/src/capture.*", True),
    ("plugin/src/sub/capture.cpp", "plugin/src/capture.*", False),
    ("tests/a/b.py", "tests/**", True), ("tests/x.py", "tests/**", True),
    ("a.md", "**/*.md", True), ("x/y/a.md", "**/*.md", True), ("x/y/a.txt", "**/*.md", False),
    ("src/calc.py", "src/calc.py", True), ("src/calc.pyc", "src/calc.py", False),
    ("src/a1.py", "src/a?.py", True), ("src/a12.py", "src/a?.py", False), ("tools\\x.py", "tools/*.py", True),
    ("anything/at/all.c", "**", True),
])
def test_glob_match(path, pattern, expected):
    assert glob_match(path, pattern) is expected


def test_spec_problems():
    assert CodeSpec.problems({"allowed_paths": ["a.py"], "acceptance": ["pytest"]}) == []
    assert any("acceptance" in p for p in CodeSpec.problems({"allowed_paths": ["a.py"]}))
    assert any("allowed_paths is empty" in p for p in CodeSpec.problems({"acceptance": ["x"]}))
    assert any("too broad" in p for p in CodeSpec.problems({"allowed_paths": ["**"], "acceptance": ["x"]}))
    assert CodeSpec.problems("nonsense")


# ------------------------------------------------- worktrees, integration branch, release

def test_accepted_work_lands_on_integration_and_only_reaches_main_on_release(repo, repo_dir):
    fix(repo)
    assert repo.commit_all("T1", "T1: fix add") and repo.commit_all("T1", "again") is None
    assert repo.changed_files("T1") == [("M", "src/calc.py")] and "+    return a + b" in repo.diff("T1")
    out = repo.merge_to_integration("T1", "Merge T1")
    assert out["merge_commit"]
    assert "a - b" in (repo_dir / "src" / "calc.py").read_text()                     # main untouched
    assert "a + b" in show(repo_dir, "orchestrator/integration:src/calc.py")
    assert not repo.has_worktree("T1") and git(repo_dir, "branch", "--list", "ticket/T1").strip()   # branch kept
    status = repo.integration_status()
    assert status["exists"] and status["ahead"] >= 1 and any("T1" in c for c in status["commits"])
    rel = repo.release("Release")
    assert "a + b" in (repo_dir / "src" / "calc.py").read_text() and rel["into"] == "main"
    assert not git(repo_dir, "branch", "--list", "orchestrator/integration").strip()   # cleaned up
    assert not git(repo_dir, "branch", "--list", "ticket/T1").strip()
    assert not repo.integration_status()["exists"]


def test_later_tickets_build_on_accepted_work_and_diff_only_their_own_change(repo):
    fix(repo)
    repo.commit_all("T1", "T1")
    repo.merge_to_integration("T1", "Merge T1")
    wt2 = repo.ensure_worktree("T2")
    assert "a + b" in (wt2 / "src" / "calc.py").read_text()                           # sees T1's accepted fix
    (wt2 / "src" / "other.py").write_text("X = 2\n")
    repo.commit_all("T2", "T2")
    assert repo.changed_files("T2") == [("M", "src/other.py")]                        # only its own change
    assert "calc.py" not in repo.diff("T2")


def test_commits_on_main_are_synced_into_integration_for_new_work(repo, repo_dir):
    repo.ensure_integration()
    (repo_dir / "src" / "other.py").write_text("X = 99\n")
    git(repo_dir, "commit", "-q", "-am", "human commit on main")
    wt = repo.ensure_worktree("T3")
    assert "X = 99" in (wt / "src" / "other.py").read_text()


def test_release_refuses_dirty_tree_wrong_branch_or_nothing_to_release(repo, repo_dir):
    with pytest.raises(CodeError, match="nothing to release"):
        repo.release("r")
    fix(repo)
    repo.commit_all("T1", "T1")
    repo.merge_to_integration("T1", "Merge T1")
    (repo_dir / "src" / "other.py").write_text("uncommitted\n")
    with pytest.raises(CodeError, match="uncommitted changes"):
        repo.release("r")
    git(repo_dir, "checkout", "-q", "--", "src/other.py")
    git(repo_dir, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(CodeError, match="not 'main'"):
        repo.release("r")


def test_conflicting_branches_abort_cleanly_and_stay_for_rework(repo, repo_dir):
    fix(repo, "T1")
    fix(repo, "T2", "def add(a, b):\n    return a * b\n")                            # both forked from the same base
    repo.commit_all("T1", "T1")
    repo.commit_all("T2", "T2")
    repo.merge_to_integration("T1", "Merge T1")
    with pytest.raises(CodeError, match="merge conflict"):
        repo.merge_to_integration("T2", "Merge T2")
    integ = repo.integration_path()
    assert git(integ, "status", "--porcelain").strip() == "" and "a + b" in (integ / "src" / "calc.py").read_text()
    assert git(repo_dir, "branch", "--list", "ticket/T2").strip()


def test_failing_integration_checks_undo_the_merge(repo):
    fix(repo)
    repo.commit_all("T1", "T1")
    repo.ensure_integration()
    before = git(repo.integration_path(), "rev-parse", "HEAD")
    with pytest.raises(CodeError, match="integration checks failed"):
        repo.merge_to_integration("T1", "Merge T1", ["python -c \"import sys; sys.exit(1)\""])
    assert git(repo.integration_path(), "rev-parse", "HEAD") == before


def test_parallel_worktree_creation_is_safe(repo):
    errors = []

    def make(i):
        try:
            repo.ensure_worktree(f"P{i}")
        except Exception as e:                                                        # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=make, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and all(repo.has_worktree(f"P{i}") for i in range(6))


def test_discard_removes_worktree_and_branch(repo, repo_dir):
    fix(repo)
    repo.discard("T1")
    assert not repo.has_worktree("T1") and not git(repo_dir, "branch", "--list", "ticket/T1").strip()
    repo.discard("T1")
    assert repo.ensure_worktree("T1").exists()


# ------------------------------------------------------------------------------ gates

def gate(gates, name):
    return next(g for g in gates if g["name"].startswith(name))


def test_gates_pass_for_a_good_change_and_fail_for_the_unfixed_test(repo):
    fix(repo, "T1", BUGGY + "# touched\n")
    repo.commit_all("T1", "T1")
    gates = run_gates(repo, "T1", SPEC, {})
    assert gate(gates, "path policy")["ok"] and gate(gates, "secret scan")["ok"]
    assert not gate(gates, "acceptance")["ok"] and "assert" in gate(gates, "acceptance")["detail"]
    fix(repo, "T2")
    repo.commit_all("T2", "T2")
    assert all(g["ok"] for g in run_gates(repo, "T2", SPEC, {}))


def test_out_of_scope_changes_are_flagged_for_review_not_blocked_and_empty_work_fails(repo):
    wt = fix(repo)
    (wt / "README.md").write_text("docs\n")
    repo.commit_all("T1", "T1")
    g = gate(run_gates(repo, "T1", SPEC, {}), "path policy")
    assert g["ok"] and g["warn"] and "README.md" in g["detail"]                      # the reviewer must agree
    repo.ensure_worktree("EMPTY")
    empty = gate(run_gates(repo, "EMPTY", SPEC, {}), "path policy")
    assert not empty["ok"] and "no files were changed" in empty["detail"]


def test_secret_scan_catches_keys_and_env_files(repo):
    spec = CodeSpec(["src/*", "src/.env.*"], ["python -c \"pass\""])
    for name, body in {"a": 'API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n', "b": "-----BEGIN RSA PRIVATE KEY-----\n",
                       "c": "token = sk-" + "a1B2c3D4" * 4 + "\n", "d": "x = 1\n"}.items():
        wt = repo.ensure_worktree(name)
        (wt / "src" / f"{name}.py").write_text(body)
        repo.commit_all(name, name)
        assert gate(run_gates(repo, name, spec, {}), "secret scan")["ok"] is (name == "d"), name
    wt = repo.ensure_worktree("e")
    (wt / "src" / ".env.local").write_text("X=1\n")
    repo.commit_all("e", "e")
    assert not gate(run_gates(repo, "e", spec, {}), "secret scan")["ok"]


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


def test_code_worker_tools(box):
    for tool in ("run_python", "pip_install", "claim_resource", "acquire_git"):
        assert "not available for code tickets" in box.call(tool, {})["error"]
    d = box.describe()
    assert "edit_file" in d and "run_command" in d and "request_work" in d and PYTEST_CMD in d
    assert box.call("run_command", {"command": "python -c \"print(6*7)\""})["detail"].endswith("42")


def test_reads_listing_and_search(box):
    assert "return a - b" in box.call("read_file", {"path": "src/calc.py"})["content"]
    ranged = box.call("read_file", {"path": "src/calc.py", "start_line": 2, "end_line": 2})
    assert ranged["content"] == "2:     return a - b" and ranged["lines"] == "2-2 of 2"
    names = {e["name"] for e in box.call("list_dir", {"path": "."})["entries"]}
    assert {"src/", "tests/"} <= names and ".git/" not in names and ".env" not in names
    hits = box.call("search_text", {"pattern": r"def \w+\(", "glob": "**/*.py"})["matches"]
    assert any(h.startswith("src/calc.py:1:") for h in hits)
    assert not box.call("search_text", {"pattern": "("})["ok"]
    assert not any(".env" in h for h in box.call("search_text", {"pattern": "SECRET"})["matches"])


def test_secrets_traversal_and_git_are_off_limits(box):
    assert "secrets file" in box.call("read_file", {"path": ".env"})["error"]
    for bad in ("../outside.txt", ".git/config"):
        assert not box.call("read_file", {"path": bad})["ok"]
    assert not box.call("write_file", {"path": ".git/hooks/pre-commit", "content": "x"})["ok"]
    assert not box.call("write_file", {"path": "../escape.py", "content": "x"})["ok"]
    assert "secrets file" in box.call("write_file", {"path": "src/.env.prod", "content": "x"})["error"]


def test_writes_inside_scope_and_flagged_outside_it(box, repo, tmp_path):
    ok = box.call("write_file", {"path": "src/calc.py", "content": "def add(a, b):\r\n    return a + b\r\n"})
    assert ok["ok"] and (repo.worktree_path("T1") / "src" / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"
    outside = box.call("write_file", {"path": "tests/test_calc.py", "content": TEST + "# more\n"})
    assert outside["ok"] and box.out_of_scope == {"tests/test_calc.py"}               # full authority, but flagged
    strict = CodeToolBox(tmp_path / "ws", "w", root=box.root, spec=SPEC, strict_scope=True)
    assert "not in this ticket's allowed_paths" in strict.call("write_file", {"path": "tests/x.py", "content": "x"})["error"]
    assert "200KB" in box.call("write_file", {"path": "src/calc.py", "content": "x" * 300_000})["error"]


def test_edit_file_needs_one_exact_match(box, repo):
    assert box.call("edit_file", {"path": "src/calc.py", "old": "a - b", "new": "a + b"})["replaced"] == 1
    assert "not found" in box.call("edit_file", {"path": "src/calc.py", "old": "nope", "new": "x"})["error"]
    box.call("write_file", {"path": "src/calc.py", "content": "x = 1\nx = 1\n"})
    assert "matches 2 places" in box.call("edit_file", {"path": "src/calc.py", "old": "x = 1", "new": "x = 2"})["error"]
    assert "does not exist" in box.call("edit_file", {"path": "src/missing.py", "old": "a", "new": "b"})["error"]
    assert "must not be empty" in box.call("edit_file", {"path": "src/calc.py", "old": "", "new": "x"})["error"]


def test_run_tests_runs_the_ticket_acceptance_commands(box):
    assert box.call("run_tests", {})["all_passed"] is False
    box.call("edit_file", {"path": "src/calc.py", "old": "a - b", "new": "a + b"})
    assert box.call("run_tests", {})["all_passed"] is True


# --------------------------------------------- a fake team that answers by role, in any order

class Team:
    """A fake model that answers by role and situation, so parallel calls may arrive in any order."""

    def __init__(self, worker=None, lead=None, verifier=None):
        self.calls, self.lock = [], threading.Lock()
        self.worker, self.lead = worker or edit_then_done, lead or integrate_whatever_is_ready
        self.verifier = verifier or (lambda first: j(verdict="pass", issues=[], advice=""))

    def complete(self, *, model, system, messages, max_tokens=16000):
        with self.lock:
            self.calls.append({"model": model, "system": system, "messages": messages})
        first = messages[0]["content"]
        if "independent verifier" in system:
            text = self.verifier(first)
        elif "Role: Code Worker" in system:
            text = self.worker(first, messages)
        else:
            text = self.lead(first, messages)
        return Completion(text=text, model=model, input_tokens=100, output_tokens=10)


def steps(messages):
    return sum(1 for m in messages if m["role"] == "assistant")


EDIT = j(action="tool", tool="edit_file", args={"path": "src/calc.py", "old": "a - b", "new": "a + b"})
DONE = j(action="done", result={"summary": "fixed the sign", "open_questions": []})


def edit_then_done(first, messages):
    return EDIT if steps(messages) == 0 else DONE


def integrate_whatever_is_ready(first, messages):
    if "Branches awaiting YOUR review" in first:
        return j(action="integrate", accept=re.findall(r"^## (\w+): ", first, re.M), notes="looks right")
    return j(action="done", result={"report": "integrated", "follow_ups": []})


WORKER_TICKET = dict(
    title="Fix add", worker_role="code_worker", context="add() subtracts; make it add.",
    acceptance_criteria=["add(2, 3) == 5"], return_spec={"format": "summary", "description": "what changed"},
    code={"allowed_paths": ["src/calc.py"], "acceptance": [PYTEST_CMD], "context_files": ["src/calc.py"]})


def cfg_for(repo_dir, tmp_path, review="manager"):
    cfg = load_config(None)
    cfg["code"].update(repo=str(repo_dir), base_branch="main", worktrees_dir=str(tmp_path / "worktrees"), review=review)
    cfg["departments"] = {"software": {}}
    return cfg


def lead(repo_dir, tmp_path, team, store, **cfg_kw):
    return Manager("software_manager", store, team, cfg_for(repo_dir, tmp_path, **cfg_kw), tmp_path / "ws", "You lead software.",
                   ["m"], ["w"])


def top_ticket(store, tickets):
    return store.create_ticket({"title": "Fix the calculator", "assignee": "software_manager", "worker_role": "worker",
                                "context": "c", "acceptance_criteria": ["a"],
                                "return_spec": {"format": "f", "description": "d"}, "worker_tickets": tickets},
                               "top_manager")["id"]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# -------------------------------------------------- the Software manager owns the repository

def test_the_software_manager_reviews_and_integrates_its_engineers_work(repo_dir, tmp_path, store):
    team = Team()
    m = lead(repo_dir, tmp_path, team, store)
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done"
    kid = store.list_tickets(parent_id=top)[0]
    assert branch_state(store, kid["id"]) == "accepted"
    accepted = [e for e in store.events(kid["id"]) if e["type"] == "code_accepted"][0]["payload"]
    assert accepted["by"] == "software_manager" and accepted["into"] == "orchestrator/integration"
    assert "a - b" in (repo_dir / "src" / "calc.py").read_text()                         # main untouched until release
    assert "a + b" in show(repo_dir, "orchestrator/integration:src/calc.py")
    review = next(c for c in team.calls if "Branches awaiting YOUR review" in c["messages"][0]["content"])["messages"][0]["content"]
    assert "+    return a + b" in review and "fixed the sign" in review                     # a real review packet


def test_the_manager_cannot_finish_with_unreviewed_branches(repo_dir, tmp_path, store):
    said = []

    def lazy_lead(first, messages):
        if "cannot finish yet" in first:
            said.append("told")
            return integrate_whatever_is_ready(first, messages)
        if "Branches awaiting YOUR review" in first and not said:
            return j(action="done", result={"report": "skipping review"})
        return integrate_whatever_is_ready(first, messages)

    m = lead(repo_dir, tmp_path, Team(lead=lazy_lead), store)
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done" and said
    assert branch_state(store, store.list_tickets(parent_id=top)[0]["id"]) == "accepted"


def test_rejected_branches_are_redone_by_a_new_engineer_with_the_feedback(repo_dir, tmp_path, store):
    rejected = []

    def picky_lead(first, messages):
        ids = re.findall(r"^## (\w+): ", first, re.M)
        if ids and not rejected:
            rejected.extend(ids)
            return j(action="integrate", reject=[{"ticket_id": ids[0], "feedback": "also add a docstring"}])
        return integrate_whatever_is_ready(first, messages)

    team = Team(lead=picky_lead)
    m = lead(repo_dir, tmp_path, team, store)
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done"
    kids = store.list_tickets(parent_id=top)
    assert len(kids) == 2 and branch_state(store, kids[0]["id"]) == "rejected" and branch_state(store, kids[1]["id"]) == "accepted"
    assert "also add a docstring" in kids[1]["context"] and kids[1]["assignee"] == "worker:code_worker-2"


def test_the_manager_can_fix_a_branch_itself_and_its_fix_is_rechecked(repo_dir, tmp_path, store):
    def half_worker(first, messages):                     # the engineer leaves a test-breaking style nit
        if steps(messages) == 0:
            return j(action="tool", tool="edit_file", args={"path": "src/calc.py", "old": "a - b", "new": "a + b  # TODO"})
        return DONE

    fixed = []

    def hands_on_lead(first, messages):
        if "Branches awaiting YOUR review" in first and not fixed:
            tid = re.findall(r"^## (\w+): ", first, re.M)[0]
            if steps(messages) == 0:
                return j(action="tool", tool="branch_edit", args={"ticket": tid, "path": "src/calc.py",
                                                                  "old": "a + b  # TODO", "new": "a + b"})
            fixed.append(tid)
            return j(action="integrate", accept=[tid])
        return integrate_whatever_is_ready(first, messages)

    team = Team(worker=half_worker, lead=hands_on_lead)
    m = lead(repo_dir, tmp_path, team, store)
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done" and fixed
    assert "TODO" not in show(repo_dir, "orchestrator/integration:src/calc.py")          # the manager's fix landed
    gates = [e for e in store.events(fixed[0]) if e["type"] == "code_gates"]
    assert len(gates) == 1                                                               # worker's own gates...
    assert "branch_edit" in [e["payload"]["tool"] for e in store.events(top) if e["type"] == "tool_call"]


def test_a_finished_branch_is_reviewed_while_a_slow_engineer_is_still_working(repo_dir, tmp_path, store):
    def worker(first, messages):
        if "Slow" in first:
            time.sleep(1.5)
            if steps(messages) == 0:
                return j(action="tool", tool="write_file", args={"path": "src/other.py", "content": "X = 2\n"})
            return DONE
        return edit_then_done(first, messages)

    team = Team(worker=worker)
    m = lead(repo_dir, tmp_path, team, store)
    slow = {**WORKER_TICKET, "title": "Slow change", "code": {"allowed_paths": ["src/other.py"], "acceptance": [PYTEST_CMD + " -k nothing || exit 0"]}}
    top = top_ticket(store, [WORKER_TICKET, slow])
    assert m.work(top)["status"] == "done"
    fast_id, slow_id = [k["id"] for k in store.list_tickets(parent_id=top)]
    seq = {(e["ticket_id"], e["type"]): e["seq"] for e in store.events() if e["type"] in ("code_accepted", "code_ready")}
    assert seq[(fast_id, "code_accepted")] < seq[(slow_id, "code_ready")]                 # reviewed before the slow one was done


def test_gate_failures_go_back_to_the_same_conversation_until_fixed(repo_dir, tmp_path, store):
    def worker(first, messages):
        n = steps(messages)
        return DONE if n == 0 else (EDIT if n == 1 else DONE)                             # says done too early, then fixes

    m = lead(repo_dir, tmp_path, Team(worker=worker), store)
    top = top_ticket(store, [WORKER_TICKET])
    assert m.work(top)["status"] == "done"
    kid = store.list_tickets(parent_id=top)[0]
    rejected = [e for e in store.events(kid["id"]) if e["type"] == "done_rejected"]
    assert len(rejected) == 1 and "no files were changed" in rejected[0]["payload"]["feedback"]


def test_a_worker_that_never_passes_escalates_and_its_work_is_kept(repo_dir, tmp_path, store):
    def wrong(first, messages):
        return j(action="tool", tool="edit_file", args={"path": "src/calc.py", "old": "a - b", "new": "a * b"}) \
            if steps(messages) == 0 else DONE

    def giving_up_lead(first, messages):
        return j(action="escalate", reason="worker could not pass") if "Worker results" in first else \
            integrate_whatever_is_ready(first, messages)

    m = lead(repo_dir, tmp_path, Team(worker=wrong, lead=giving_up_lead), store)
    top = top_ticket(store, [WORKER_TICKET])
    m.work(top)
    kid = store.list_tickets(parent_id=top)[0]
    assert kid["status"] == "escalated" and "still failing its checks after 3 attempts" in kid["escalation"]["reason"]
    assert (tmp_path / "worktrees" / kid["id"]).exists()


def test_bad_code_specs_and_non_software_departments_are_refused(repo_dir, tmp_path, store):
    m = lead(repo_dir, tmp_path, Team(), store)
    top = top_ticket(store, [])
    with pytest.raises(Exception, match="acceptance is empty"):
        m._delegate(top, {"tickets": [WORKER_TICKET, {**WORKER_TICKET, "title": "bad", "code": {"allowed_paths": ["src/x.py"]}}]})
    assert store.list_tickets(parent_id=top) == []
    research = Manager("research_manager", store, Team(), cfg_for(repo_dir, tmp_path), tmp_path / "ws", "r", ["m"], ["w"])
    with pytest.raises(Exception, match="only the Software department writes code"):
        research._delegate(top, {"tickets": [WORKER_TICKET]})


def test_verifiers_review_the_actual_patch_and_the_rules(repo_dir, tmp_path, store, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "RULES.md").write_text("1. No decompiled code in the repo.", encoding="utf-8")
    cfg = cfg_for(repo_dir, tmp_path)
    cfg["rules_file"] = "RULES.md"
    from orchestrator.verification import Verifier
    team = Team()
    v = Verifier(store, team, cfg, tmp_path / "ws", ["verifier-model"])
    m = Manager("software_manager", store, team, cfg, tmp_path / "ws", "role", ["m"], ["w"], verifier=v)
    assert m.work(top_ticket(store, [WORKER_TICKET]))["status"] == "done"
    seen = next(c for c in team.calls if "independent verifier" in c["system"])["messages"][0]["content"]
    assert "PATCH for" in seen and "+    return a + b" in seen and "No decompiled code in the repo" in seen


# -------------------------------------------- the top manager can still review (review: top_manager)

def make_ctl(repo_dir, tmp_path, team, review="top_manager"):
    cfg = cfg_for(repo_dir, tmp_path, review=review)
    cfg["workspace"] = str(tmp_path / "ws")
    return Ctl(cfg, tmp_path / "ws", team)


def create(ctl, tickets=None):
    return ctl.create(dict(department="software", title="Fix the calculator", context="c", acceptance_criteria=["a"],
                           return_spec={"format": "f", "description": "d"}, worker_tickets=tickets or [WORKER_TICKET]))["ticket_id"]


def test_top_manager_review_accept_integration_and_release(repo_dir, tmp_path):
    ctl = make_ctl(repo_dir, tmp_path, Team())
    create(ctl)
    ctl.run()
    pending = ctl.code_list()["awaiting_review"]
    assert len(pending) == 1 and pending[0]["gates_passed"]
    tid = pending[0]["id"]
    packet = ctl.code_show(tid)
    assert packet["summary"] == "fixed the sign" and "+    return a + b" in packet["diff"]
    out = ctl.code_accept(tid)
    assert out["into"] == "orchestrator/integration" and "a - b" in (repo_dir / "src" / "calc.py").read_text()
    assert ctl.code_integration()["ahead"] >= 1
    rel = ctl.code_release()
    assert rel["into"] == "main" and "a + b" in (repo_dir / "src" / "calc.py").read_text()
    with pytest.raises(Exception, match="not awaiting code review"):
        ctl.code_accept(tid)
    ctl.close()


def test_top_manager_reject_and_discard(repo_dir, tmp_path):
    ctl = make_ctl(repo_dir, tmp_path, Team())
    create(ctl)
    ctl.run()
    tid = ctl.code_list()["awaiting_review"][0]["id"]
    with pytest.raises(Exception, match="needs feedback"):
        ctl.code_reject(tid, "  ")
    out = ctl.code_reject(tid, "Also handle floats.")
    redo = ctl.store.get_ticket(out["replacement_ticket"])
    assert redo["title"].startswith("Redo:") and "Also handle floats" in redo["worker_tickets"][0]["context"]
    ctl.run()
    again = ctl.code_list()["awaiting_review"]
    assert len(again) == 1 and again[0]["id"] != tid
    assert ctl.code_discard(again[0]["id"]) == {"discarded": again[0]["id"]}
    with pytest.raises(Exception, match="nope-123"):
        ctl.code_show("nope-123")
    ctl.close()


def test_the_dashboard_lists_code_awaiting_review_and_integration(repo_dir, tmp_path):
    from orchestrator.dashboard import build_state
    ctl = make_ctl(repo_dir, tmp_path, Team())
    create(ctl)
    ctl.run()
    db = tmp_path / "ws" / "orchestrator.db"
    assert [c["files"] for c in build_state(db, ctl.cfg)["code_pending"]] == [["src/calc.py"]]
    ctl.code_accept(ctl.code_list()["awaiting_review"][0]["id"])
    s = build_state(db, ctl.cfg)
    assert s["code_pending"] == [] and s["integration"]["ahead"] >= 1
    ctl.close()


def test_the_scaffold_includes_a_software_department(tmp_path):
    from pathlib import Path
    from orchestrator.init import scaffold
    scaffold(tmp_path / "proj")
    cfg = load_config(tmp_path / "proj" / "config" / "orchestrator.yaml")
    assert cfg["departments"]["software"]["manager_role"] == "roles/software_manager.md"
    root = Path(__file__).resolve().parents[1] / "roles"
    assert "Software Manager" in (root / "software_manager.md").read_text(encoding="utf-8")
    assert (root / "workers" / "code_worker.md").exists()


def test_build_output_a_worker_created_is_never_committed():
    from orchestrator.codework import is_build_output
    for p in ("build64/CMakeCache.txt", "build64/Release/nvvr_vrhost.exe", "plugin/x64/Release/a.obj",
              "src/__pycache__/m.cpython-312.pyc", "out/x.txt", "tools/foo.exe"):
        assert is_build_output(p), p
    for p in ("plugin/src/perf.cpp", "tools/perf_report.py", "README.md", "builders.md", "src/release_notes.txt"):
        assert not is_build_output(p), p


# ------------------------------------------------------------------------- acceptance commands are pre-checked

def test_preflight_flags_commands_that_cannot_run_and_passes_ones_that_merely_fail(repo, repo_dir):
    (repo_dir / "tools").mkdir()
    (repo_dir / "tools" / "needs_arg.py").write_text(
        "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('test')\nap.parse_args()\n")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-q", "-m", "tool")
    ok_fail = CodeSpec(["src/calc.py"], [PYTEST_CMD])                       # runs and fails before the fix: fine
    usage = CodeSpec(["src/calc.py"], ["python tools/needs_arg.py"])           # usage error: never passes
    missing = CodeSpec(["src/calc.py"], ["python tools/nope.py"])               # script missing, not the worker's
    own_new = CodeSpec(["tools/new_tool.py"], ["python tools/new_tool.py"])     # the worker will create it: fine
    no_prog = CodeSpec(["src/calc.py"], ["definitely-not-a-program-xyz --run"])
    assert repo.scratch_preflight([ok_fail, own_new]) == []
    bad = repo.scratch_preflight([usage, missing, no_prog])
    assert len(bad) == 3, bad
    assert "usage error" in bad[0] and "does not exist" in bad[1] and "not found" in bad[2]
    assert "orch-preflight" not in git(repo_dir, "worktree", "list")          # the scratch checkout is gone


def test_a_delegation_with_an_unrunnable_acceptance_command_is_sent_back_before_any_worker_starts(repo, repo_dir, tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = load_config(None)
    cfg["code"] = {**cfg.get("code", {}), "repo": str(repo_dir), "base_branch": "main",
                   "worktrees_dir": str(tmp_path / "worktrees")}
    first = []

    class P:
        def complete(self, *, model, system, messages, max_tokens=16000):
            u = messages[0]["content"]
            first.append(u)
            if "Plan the work" in u and len([x for x in first if "Plan the work" in x]) == 1:
                return Completion(text=j(action="delegate", tickets=[{
                    "title": "fix", "context": "c", "acceptance_criteria": ["a"], "return_spec": {"format": "f", "description": "d"},
                    "code": {"allowed_paths": ["src/calc.py"], "acceptance": ["definitely-not-a-program-xyz"]}}]), model=model)
            return Completion(text=j(action="done", result={"report": "gave up"}), model=model)

    m = Manager("software_manager", store, P(), cfg, tmp_path, "role", ["m"], ["w"], writes_code=True)
    t = store.create_ticket({"title": "T", "assignee": "software_manager", "worker_role": "w", "context": "c",
                             "acceptance_criteria": ["a"], "return_spec": {"format": "f", "description": "d"}}, "top_manager")
    m.work(t["id"])
    assert store.list_tickets(parent_id=t["id"]) == []                        # nothing was delegated
    assert any("cannot run as written" in u for u in first)                   # the manager was told why
    store.close()
