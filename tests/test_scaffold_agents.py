from orchestrator.init import agents_text, command_text, scaffold


def test_scaffold_writes_agents_md_for_codex_and_binds_the_project(tmp_path):
    root = tmp_path / "proj"
    written = {p.name for p in scaffold(root)}
    assert {"AGENTS.md", "orchestrate.md", "orca.yaml"} <= written
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    proj = str(root.resolve())
    assert agents.startswith("# Orchestrator project: director instructions")
    assert "argument-hint" not in agents and "$ARGUMENTS" not in agents          # slash-command bits removed
    assert "whatever they ask you next" in agents
    assert "Choosing models" in agents and "use-cli codex" in agents
    assert f'python -P -m orchestrator.ctl --project "{proj}" models' in agents      # every command bound to the project
    assert f'python -P -m orchestrator.smoke --project "{proj}"' in agents


def test_claude_command_and_agents_md_share_the_same_instructions(tmp_path):
    cmd, agents = command_text(tmp_path), agents_text(tmp_path)
    for needle in ("Choosing models", "Recovery after a crash", "Escalation hierarchy", "Watch, don't poll",
                   "ctl.*human", "handoff"):
        if needle == "ctl.*human":
            assert "orchestrator.ctl" in cmd and " human " in cmd and " human " in agents
        else:
            assert needle in cmd and needle in agents
    assert "$ARGUMENTS" in cmd and "$ARGUMENTS" not in agents


def test_instructions_explain_the_same_model_for_everything_setup(tmp_path):
    text = command_text(tmp_path)
    assert "Everything on the same model as you" in text
    assert "use-cli claude" in text and "use-cli codex" in text
    assert "no API key is needed" in text.lower() or "No API key is needed" in text
    assert "config/models.yaml" in text
