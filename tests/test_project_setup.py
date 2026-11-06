import json
import subprocess
import sys

import pytest

from orchestrator import Store
from orchestrator.config import load_config, model_chain
from orchestrator.ctl import Ctl, main
from orchestrator.init import scaffold
from orchestrator.manager import load_role
from orchestrator.providers import MockProvider
from orchestrator.director import Director


def j(**kw):
    return json.dumps(kw)


# ------------------------------------------------------------ project-level roles

def test_project_roles_override_and_extend_the_built_in_ones(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "focused" in load_role("my_custom_worker")                        # unknown role: generic fallback
    assert "Tool Scout" in load_role("tool_scout")                           # built-in worker role
    (tmp_path / "roles" / "workers").mkdir(parents=True)
    (tmp_path / "roles" / "workers" / "my_custom_worker.md").write_text("You are MY worker.", encoding="utf-8")
    (tmp_path / "roles" / "workers" / "tool_scout.md").write_text("PROJECT scout prompt", encoding="utf-8")
    assert load_role("my_custom_worker") == "You are MY worker."
    assert load_role("tool_scout") == "PROJECT scout prompt"                 # the project wins over the built-in


def test_a_department_can_use_a_project_manager_role_or_a_new_department(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "gamedev_manager.md").write_text("You manage GAME DEVELOPMENT for this project.",
                                                           encoding="utf-8")
    cfg = load_config(None)
    cfg["departments"] = {"gamedev": {"manager_role": "roles/gamedev_manager.md"},
                          "research": {"manager_role": "roles/research_manager.md"},      # built-in, found in the repo
                          "art": {"mission": "make placeholder art"}}                      # no file: template + mission
    store = Store(tmp_path / "t.db")
    top = Director(store, MockProvider([]), cfg, tmp_path / "ws")
    assert "GAME DEVELOPMENT" in top.managers["gamedev"].role_text
    assert "Research Manager" in top.managers["research"].role_text
    assert "make placeholder art" in top.managers["art"].role_text
    store.close()


# ------------------------------------------------------------------- hardware setup

def test_hardware_can_be_declared_from_the_command_line(tmp_path, capsys):
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text(f"workspace: {json.dumps(str(tmp_path / 'ws'))}\ndepartments:\n  experiment: {{resource_leases: true}}\n")
    assert main(["--config", str(cfgfile), "add-resource", "thermal-rig", "stop between sweep points"],
                provider=MockProvider([])) == 0
    assert json.loads(capsys.readouterr().out)["resources"] == ["thermal-rig"]
    assert main(["--config", str(cfgfile), "safe-to-stop", "thermal-rig", "never while the heater is on"],
                provider=MockProvider([])) == 0
    capsys.readouterr()
    cfg = load_config(cfgfile)
    ctl = Ctl(cfg, cfg["workspace"], MockProvider([]))
    r = ctl.store.list_resources()[0]
    assert r["name"] == "thermal-rig" and r["safe_to_stop"] == "never while the heater is on"
    ctl.close()


def test_unknown_resource_note_is_a_clean_error(tmp_path, capsys):
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text(f"workspace: {json.dumps(str(tmp_path / 'ws'))}\n")
    assert main(["--config", str(cfgfile), "safe-to-stop", "ghost", "x"], provider=MockProvider([])) == 1
    assert "ghost" in capsys.readouterr().out


# --------------------------------------------------------------------- init --use-cli

@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_init_use_cli_scaffolds_straight_onto_that_agent(tmp_path, cli):
    root = tmp_path / "proj"
    r = subprocess.run([sys.executable, "-m", "orchestrator.init", str(root), "--use-cli", cli],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"your {cli} CLI" in r.stdout
    cfg = load_config(root / "config" / "orchestrator.yaml")
    for tier in ("manager", "worker"):
        assert model_chain(cfg, tier) == [f"{cli}-cli:default"]
    assert cfg["verification"]["verifier_models"] == [f"{cli}-cli:default"]
    assert cfg["models"]["fallback_chain"] == []
    assert (root / "AGENTS.md").exists() and (root / ".claude" / "commands" / "orchestrate.md").exists()
    assert (root / "orca.yaml").exists() and not (root / "config").exists()      # one file to edit


def test_init_without_use_cli_leaves_the_default_scaffold(tmp_path):
    root = tmp_path / "plain"
    scaffold(root)
    assert not (root / "config" / "models.yaml").exists()
