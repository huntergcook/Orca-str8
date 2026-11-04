import json
import sys
from pathlib import Path

import httpx
import pytest

from orchestrator.config import load_config, model_chain, overlay_path
from orchestrator.ctl import Ctl, main
from orchestrator.modelcfg import current, detect, set_models, use_cli
from orchestrator.providers import (CliProvider, MockProvider, OpenAICompatProvider, RoutingProvider, build_provider,
                                    flatten_prompt, requirements)

MSGS = [{"role": "user", "content": "Plan the work."}]


def fake_which(found):
    return lambda exe: f"/bin/{exe}" if exe in found else None


def capture(returncode=0, stdout="reply text", stderr="", write_outfile=None):
    calls = []

    def run(cmd, input_text, timeout, cwd):
        calls.append({"cmd": cmd, "input": input_text})
        if write_outfile is not None and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text(write_outfile, encoding="utf-8")
        return returncode, stdout, stderr
    return run, calls


# ------------------------------------------------------------------ CLI backends

def test_claude_preset_runs_as_plain_completion_with_prompt_on_stdin():
    run, calls = capture(stdout="  {\"action\": \"done\"}  \n")
    p = CliProvider(preset="claude", run=run, which=fake_which({"claude"}))
    c = p.complete(model="default", system="You manage research.", messages=MSGS)
    cmd = calls[0]["cmd"]
    assert cmd[0] == "/bin/claude" and "-p" in cmd and cmd[cmd.index("--tools") + 1] == ""   # tools disabled
    assert "--no-session-persistence" in cmd and "--model" not in cmd                       # CLI's own default model
    assert "You manage research." in calls[0]["input"] and "Plan the work." in calls[0]["input"]
    assert c.text == '{"action": "done"}' and c.model == "default" and c.input_tokens > 0


def test_claude_preset_passes_an_explicit_model():
    run, calls = capture()
    CliProvider(preset="claude", run=run, which=fake_which({"claude"})).complete(
        model="sonnet", system="s", messages=MSGS)
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_codex_preset_reads_the_final_message_file_not_the_noisy_stdout():
    run, calls = capture(stdout="banner\nprogress...\nnoise", write_outfile='{"action": "done"}')
    p = CliProvider(preset="codex", run=run, which=fake_which({"codex"}))
    c = p.complete(model="gpt-x", system="sys", messages=MSGS)
    cmd = calls[0]["cmd"]
    assert cmd[:3] == ["/bin/codex", "exec", "--skip-git-repo-check"] and "--ephemeral" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"                       # no edits, plain completion
    assert cmd[cmd.index("-m") + 1] == "gpt-x" and cmd[-1] == "-"               # prompt comes from stdin
    assert c.text == '{"action": "done"}'                                       # from -o file, not stdout
    run2, calls2 = capture(write_outfile="x")
    CliProvider(preset="codex", run=run2, which=fake_which({"codex"})).complete(model="default", system="s", messages=MSGS)
    assert "-m" not in calls2[0]["cmd"]


def test_custom_command_with_model_placeholder_and_argument_prompt():
    run, calls = capture()
    p = CliProvider(command=["mytool", "--model", "{model}"], input="arg", run=run, which=fake_which({"mytool"}))
    p.complete(model="m1", system="s", messages=MSGS)
    cmd = calls[0]["cmd"]
    assert cmd[1:3] == ["--model", "m1"] and "Plan the work." in cmd[-1] and calls[0]["input"] == ""


def test_cli_failures_are_clear():
    run, _ = capture(returncode=2, stderr="not logged in")
    with pytest.raises(RuntimeError, match="exited with code 2: not logged in"):
        CliProvider(preset="claude", run=run, which=fake_which({"claude"})).complete(model="d", system="s", messages=MSGS)
    run2, _ = capture(stdout="   ")
    with pytest.raises(RuntimeError, match="returned no text"):
        CliProvider(preset="claude", run=run2, which=fake_which({"claude"})).complete(model="d", system="s", messages=MSGS)
    with pytest.raises(RuntimeError, match="not found on PATH"):
        CliProvider(preset="codex", run=run, which=fake_which(set())).complete(model="d", system="s", messages=MSGS)
    with pytest.raises(ValueError, match="unknown CLI preset"):
        CliProvider(preset="nope")
    with pytest.raises(ValueError):
        CliProvider()


def test_flatten_prompt_keeps_roles_in_order():
    t = flatten_prompt("be brief", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                    {"role": "user", "content": "c"}])
    assert t.index("be brief") < t.index("## USER") < t.index("## ASSISTANT") < t.rindex("## USER")


# ------------------------------------------------------------------- routing

def test_two_clis_with_the_same_model_name_route_separately():
    a, b = MockProvider(["from-claude"]), MockProvider(["from-codex"])
    r = RoutingProvider({"claude-cli": lambda: a, "codex-cli": lambda: b}, {})
    out = r.complete(model="codex-cli:default", system="s", messages=[])
    assert out.text == "from-codex" and out.model == "codex-cli:default"
    assert b.calls[0]["model"] == "default"                                      # provider sees the bare model
    assert r.complete(model="claude-cli:default", system="s", messages=[]).text == "from-claude"
    assert r.resolve("llama3:8b") == ("anthropic", "llama3:8b")                  # ollama-style tags are untouched


def test_local_openai_compatible_server_needs_no_key():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}})

    p = OpenAICompatProvider(api_key_env="", base_url="http://localhost:11434/v1", require_key=False,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.complete(model="llama3", system="s", messages=[]).text == "hi"
    assert seen["auth"] is None and seen["url"] == "http://localhost:11434/v1/chat/completions"


# -------------------------------------------------------------------- config

def test_cli_models_are_qualified_in_chains_and_defaults_exist():
    cfg = load_config(None)
    for name in ("claude-cli", "codex-cli", "ollama", "openai", "deepseek", "anthropic"):
        assert name in cfg["providers"]
    cfg["models"]["worker"] = {"provider": "codex-cli", "model": "default"}
    cfg["models"]["fallback_chain"] = [{"provider": "claude-cli", "model": "sonnet"}, {"provider": "deepseek", "model": "m"}]
    assert model_chain(cfg, "worker") == ["codex-cli:default", "claude-cli:sonnet", "m"]


def test_requirements_report_missing_keys_and_clis(monkeypatch):
    cfg = load_config(None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda exe: None)
    need = requirements(cfg, ["deepseek-v4-pro", "codex-cli:default", "claude-cli:default", "llama3"])
    assert need["keys"] == ["DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"]            # llama3 falls to the anthropic default
    assert need["clis"] == ["codex", "claude"]
    monkeypatch.setattr("shutil.which", lambda exe: f"/bin/{exe}")
    assert requirements(cfg, ["codex-cli:default"]) == {"keys": [], "clis": []}
    cfg["providers"]["ollama"]["require_key"] = False
    assert requirements(cfg, ["ollama:llama3"]) == {"keys": [], "clis": []}


# ----------------------------------------------------------------- ctl models

def project(tmp_path):
    (tmp_path / "config").mkdir()
    base = tmp_path / "config" / "orchestrator.yaml"
    base.write_text("# my hand-edited config\nworkspace: ./ws\nverification: {enabled: true}\n", encoding="utf-8")
    return base


def test_detect_reports_what_this_machine_has():
    d = detect(env={"DEEPSEEK_API_KEY": "x"}, which=fake_which({"codex"}), probe=lambda url: False)
    assert d["api_keys"] == {"anthropic": False, "deepseek": True, "openai": False}
    assert d["cli"]["codex"] and d["cli"]["claude"] is None and d["local"]["ollama"] is False
    assert any(o.startswith("use-cli codex") for o in d["options"]) and not any("use-cli claude" in o for o in d["options"])
    none = detect(env={}, which=fake_which(set()), probe=lambda url: False)
    assert "nothing detected" in none["options"][0]
    assert any("ollama" in o for o in detect(env={}, which=fake_which(set()), probe=lambda url: True)["options"])


def test_use_cli_makes_every_role_the_same_model_without_touching_the_base_config(tmp_path):
    base = project(tmp_path)
    before = base.read_text(encoding="utf-8")
    out = use_cli(base, "codex", "default")
    assert base.read_text(encoding="utf-8") == before                           # hand-edited file untouched
    assert overlay_path(base).exists()
    cfg = load_config(base)
    assert model_chain(cfg, "manager") == ["codex-cli:default"] == model_chain(cfg, "worker")
    assert cfg["verification"]["verifier_models"] == ["codex-cli:default"]
    assert cfg["verification"]["enabled"] is True and cfg["workspace"] == "./ws"   # base values survive the merge
    assert cfg["models"]["fallback_chain"] == [] and "same model" in out["note"]


def test_set_models_validates_and_supports_fallbacks(tmp_path):
    base = project(tmp_path)
    set_models(base, ["worker"], "deepseek", "deepseek-flash", fallback=["deepseek:deepseek-v4-pro"])
    cfg = load_config(base)
    assert model_chain(cfg, "worker") == ["deepseek-flash", "deepseek-v4-pro"]
    with pytest.raises(ValueError, match="unknown provider"):
        set_models(base, ["worker"], "nonsense", "m")
    with pytest.raises(ValueError, match="unknown tier"):
        set_models(base, ["boss"], "deepseek", "m")
    with pytest.raises(ValueError, match="provider:model"):
        set_models(base, ["worker"], "deepseek", "m", fallback=["oops"])
    use_cli(base, "claude", "sonnet", ["worker"])                               # later choices layer on earlier ones
    cfg = load_config(base)
    assert model_chain(cfg, "worker") == ["claude-cli:sonnet"] and model_chain(cfg, "manager")[0] == "claude-opus-5-5"
    assert current(cfg)["worker"]["id"] == "claude-cli:sonnet"
    with pytest.raises(ValueError, match="unknown CLI"):
        use_cli(base, "gemini")


def test_overlay_applies_even_without_a_base_config(tmp_path):
    (tmp_path / "config").mkdir()
    base = tmp_path / "config" / "orchestrator.yaml"                            # does not exist
    use_cli(base, "codex")
    assert model_chain(load_config(base), "worker") == ["codex-cli:default"]


def test_models_cli_roundtrip(tmp_path, capsys):
    base = project(tmp_path)
    assert main(["--config", str(base), "models", "use-cli", "--cli", "codex", "--tiers", "worker,verifier"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["now"]["worker"]["id"] == "codex-cli:default" and out["now"]["verifier_models"] == ["codex-cli:default"]
    assert main(["--config", str(base), "models", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "current" in shown and "detected" in shown
    assert main(["--config", str(base), "models", "set"]) == 1                  # --provider required
    assert "needs --provider" in capsys.readouterr().out
    assert main(["--config", str(base), "models", "use-cli"]) == 1
    capsys.readouterr()


# ------------------------------------------- end to end through a real CLI process

FAKE_CLI = '''
import sys
text = sys.stdin.read()
if "Plan the work" in text:
    print('{"action": "delegate", "tickets": [{"title": "W", "worker_role": "r", "context": "c", '
          '"acceptance_criteria": ["a"], "return_spec": {"format": "f", "description": "d"}}]}')
elif "Review, then finish" in text:
    print('{"action": "done", "result": {"final": "all good"}}')
else:
    print('{"action": "done", "result": {"found": "it"}}')
'''


def test_whole_orchestrator_runs_on_a_cli_backend_with_no_api_keys(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    cfg = load_config(None)
    cfg["providers"]["fakecli"] = {"type": "cli", "command": [sys.executable, str(script)]}
    for tier in ("manager", "worker"):
        cfg["models"][tier] = {"provider": "fakecli", "model": "default"}
    cfg["models"]["top"] = {"provider": "external", "model": "me"}
    cfg["verification"]["enabled"] = False
    cfg["departments"] = {"research": {}}
    ctl = Ctl(cfg, tmp_path / "ws")                                             # real provider, real subprocess
    assert ctl.missing_keys() == [] and ctl.missing_clis() == []
    tid = ctl.create(dict(department="research", title="T", context="c", acceptance_criteria=["a"],
                          return_spec={"format": "f", "description": "d"}))["ticket_id"]
    out = ctl.run()
    row = next(t for t in out["tickets"] if t["id"] == tid)
    assert row["status"] == "done" and row["result"] == {"final": "all good"}
    assert ctl.store.list_tickets(parent_id=tid)[0]["result"] == {"found": "it"}
    ctl.close()
