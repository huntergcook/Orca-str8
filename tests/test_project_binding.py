"""Work stays inside the project folder wherever the process starts."""
import json
import os

from orchestrator.ctl import main
from orchestrator.init import scaffold
from orchestrator.providers import MockProvider


def make_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "config").mkdir(parents=True)
    (proj / "config" / "orchestrator.yaml").write_text("departments:\n  research: {}\n", encoding="utf-8")
    return proj


def test_project_flag_binds_workspace_and_ignores_cwd(tmp_path, capsys, monkeypatch):
    proj = make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                          # session "drifted" to another folder
    assert main(["--project", str(proj), "resume"], provider=MockProvider([])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["departments"] == ["research"]             # the project's config was found
    assert (proj / "workspace" / "orchestrator.db").exists()
    assert not (elsewhere / "workspace").exists()         # nothing leaked into the other folder


def test_project_env_var_and_missing_folder(tmp_path, capsys, monkeypatch):
    proj = make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRATOR_PROJECT", str(proj))
    assert main(["board"], provider=MockProvider([])) == 0
    assert (proj / "workspace").exists()
    capsys.readouterr()
    assert main(["--project", str(tmp_path / "nope"), "board"], provider=MockProvider([])) == 1
    assert "not found" in capsys.readouterr().out


def test_dotenv_comes_from_the_project(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    (proj / ".env").write_text("PROJ_ONLY_KEY_T=found\n", encoding="utf-8")
    monkeypatch.delenv("PROJ_ONLY_KEY_T", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["--project", str(proj), "board"], provider=MockProvider([]))
    assert os.environ.get("PROJ_ONLY_KEY_T") == "found"
    monkeypatch.delenv("PROJ_ONLY_KEY_T", raising=False)


def test_scaffolded_command_bakes_in_project_path(tmp_path):
    root = tmp_path / "myproj"
    scaffold(root)
    text = (root / ".claude" / "commands" / "orchestrate.md").read_text(encoding="utf-8")
    proj = str(root.resolve())
    assert f'python -m orchestrator.ctl --project "{proj}" resume' in text
    assert "python -m orchestrator.ctl run" not in text            # no bare, unbound ctl calls remain
    assert f"{root.resolve().as_posix()}/workspace/run.log" in text
