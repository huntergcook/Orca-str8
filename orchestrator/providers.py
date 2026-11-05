"""Provider interface and implementations. Nothing else in the runtime imports a vendor SDK.

  AnthropicProvider   Claude via the official SDK (API key)
  OpenAICompatProvider  any OpenAI-format /chat/completions API over plain httpx: DeepSeek, OpenAI,
                      and local servers (Ollama, LM Studio, vLLM) that need no key
  CliProvider         a command-line agent you already have (Claude Code, Codex, or any command) used as a
                      plain text-completion backend: no API key needed, runs on your CLI subscription
  RoutingProvider     sends each model ID to the provider that serves it
  MockProvider        scripted, for tests

CLI models are qualified as "<provider>:<model>" (e.g. "codex-cli:default") so two CLIs that both offer a
"default" model can be told apart. "default" means: do not pass a model flag, use the CLI's own default,
which is whatever you normally run it with.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx


@dataclass
class Completion:
    text: str
    refused: bool = False
    refusal_category: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cache_hit_tokens: int = 0       # input tokens served from the provider's prompt cache (cheaper)
    cache_write_tokens: int = 0     # input tokens written to the prompt cache (Anthropic: 1.25x the input rate)


class Provider(Protocol):
    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion: ...


class MockProvider:
    """Scripted provider for tests. Each call pops the next item: a string or a Completion."""

    def __init__(self, script: list[str | Completion]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        self.calls.append({"model": model, "system": system, "messages": messages})
        if not self.script:
            raise RuntimeError("MockProvider script exhausted")
        item = self.script.pop(0)
        return item if isinstance(item, Completion) else Completion(text=item, model=model)


class AnthropicProvider:
    """Claude via the official SDK. Credentials come from api_key_env if that variable is set,
    otherwise from the SDK's own resolution (ANTHROPIC_API_KEY, or an `ant auth login` profile).

    Refusals are surfaced to the caller as Completion.refused. We deliberately do NOT use
    server-side refusal fallbacks: refusal handling is policy owned by the orchestrator
    (retry once with honest context, then escalate to the human).
    """

    def __init__(self, client: Any | None = None, api_key_env: str | None = None) -> None:
        if client is None:
            import anthropic  # lazy: only needed when this provider is used
            key = os.environ.get(api_key_env) if api_key_env else None
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.client = client

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        # Prompt caching: an explicit breakpoint after the static system prompt (role, protocol, tool list: identical
        # on every call of an agent) plus top-level automatic caching, which moves a breakpoint to the end of the
        # growing conversation. Workers resend their whole conversation each call, so most input becomes cache reads.
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages,
                                  "cache_control": {"type": "ephemeral"}}
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        try:
            with self.client.messages.stream(**kwargs) as stream:   # stream: large max_tokens never time out
                msg = stream.get_final_message()
        except Exception as e:
            # out of credit, or a usage/spend limit: the caller skips this model for the rest of the run
            if getattr(e, "status_code", None) is not None and _LIMIT_RE.search(str(e)):
                raise ModelUnavailable(f"Anthropic {getattr(e, 'status_code', '')}: {str(e)[:300]}") from e
            raise
        text = "".join(b.text for b in msg.content if b.type == "text")
        refused = msg.stop_reason == "refusal"
        details = getattr(msg, "stop_details", None)
        u = msg.usage
        read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        return Completion(
            text=text,
            refused=refused,
            refusal_category=getattr(details, "category", None) if refused else None,
            # Anthropic's input_tokens excludes cached tokens; count the whole prompt, like other providers do
            input_tokens=int(u.input_tokens or 0) + read + write,
            output_tokens=int(u.output_tokens or 0),
            model=model,
            cache_hit_tokens=read,
            cache_write_tokens=write,
        )



class ModelUnavailable(RuntimeError):
    """The model cannot be used for now (subscription limit, quota, no balance). Callers skip to the next model."""


UNAVAILABLE: dict[str, str] = {}          # model id -> why; process-wide, since quotas are account-wide
_UNAVAILABLE_LOCK = threading.Lock()
_LIMIT_RE = re.compile(r"hit your .{0,40}limit|usage limit|insufficient.{0,20}balance|quota|credit balance|"
                       r"out of credits|billing", re.I)


def mark_unavailable(model: str, why: str) -> None:
    with _UNAVAILABLE_LOCK:
        UNAVAILABLE[model] = why[:300]


def usable(models: list[str]) -> list[str]:
    """The models of a chain that are not marked unavailable (order kept)."""
    return [m for m in models if m not in UNAVAILABLE]


def balance_url(spec: dict[str, Any]) -> str | None:
    """Where a provider reports the account's remaining money: `balance_url` in its config, or DeepSeek's
    /user/balance when the base URL is DeepSeek's. None if the provider has no such endpoint."""
    if spec.get("balance_url"):
        return str(spec["balance_url"])
    base = str(spec.get("base_url") or "")
    return base.rstrip("/") + "/user/balance" if "api.deepseek.com" in base else None


def fetch_balances(cfg: dict[str, Any], http_get: Callable[..., Any] | None = None) -> dict[str, dict[str, Any]]:
    """{provider: {"usd": float, "available": bool} or {"error": "..."}} for every configured provider that reports
    a balance. The API key is sent only to that provider's own endpoint and is never returned or logged."""
    out: dict[str, dict[str, Any]] = {}
    get = http_get or (lambda url, headers: httpx.get(url, headers=headers, timeout=20))
    for name, spec in (cfg.get("providers") or {}).items():
        url = balance_url(spec or {})
        if not url:
            continue
        key = os.environ.get(str(spec.get("api_key_env") or ""), "")
        if not key:
            out[name] = {"error": f"{spec.get('api_key_env')} is not set"}
            continue
        try:
            r = get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
            data = r.json()
            infos = data.get("balance_infos") or []
            usd = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
            if usd is None:
                out[name] = {"error": f"no balance in reply (HTTP {r.status_code})"}
                continue
            out[name] = {"usd": float(usd.get("total_balance", 0)), "currency": usd.get("currency", "?"),
                         "available": bool(data.get("is_available", True))}
        except Exception as e:                         # network, JSON, unexpected shape
            out[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out

class OpenAICompatProvider:
    """Any OpenAI-format API (POST {base_url}/chat/completions): DeepSeek, OpenAI, Together, and local
    servers such as Ollama (http://localhost:11434/v1) that need no key (require_key=False).

    Only the final `content` is returned (reasoning_content is dropped). A `content_filter` finish is
    treated as a refusal so it follows the same retry-once-then-escalate policy.
    """

    def __init__(self, api_key: str | None = None, api_key_env: str = "DEEPSEEK_API_KEY",
                 base_url: str = "https://api.deepseek.com", max_output_tokens: int = 32000,
                 client: Any | None = None, retries: int = 3, backoff: float = 2.0,
                 require_key: bool = True) -> None:
        self.api_key = api_key or os.environ.get(api_key_env or "")
        self.api_key_env = api_key_env
        self.require_key = require_key
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.retries, self.backoff = retries, backoff
        if client is None:
            import httpx
            client = httpx.Client(timeout=600)
        self.client = client

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        if self.require_key and not self.api_key:
            raise RuntimeError(f"no API key: set the {self.api_key_env} environment variable")
        body = {"model": model, "max_tokens": min(max_tokens, self.max_output_tokens), "stream": False,
                "messages": [{"role": "system", "content": system}, *messages]}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = ""
        for attempt in range(self.retries):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            except Exception as e:
                # A dropped connection, read timeout or similar is normal over long generations: retry it like a 5xx.
                if not _is_transport_error(e):
                    raise
                last = type(e).__name__
                time.sleep(self.backoff * (2 ** attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(self.backoff * (2 ** attempt))
                continue
            if r.status_code == 402 or (r.status_code >= 400 and _LIMIT_RE.search(r.text or "")):
                raise ModelUnavailable(f"API error {r.status_code} from {self.base_url}: {r.text[:300]}")
            if r.status_code >= 400:
                raise RuntimeError(f"API error {r.status_code} from {self.base_url}: {r.text[:300]}")
            data = r.json()
            choice = data["choices"][0]
            refused = choice.get("finish_reason") == "content_filter"
            usage = data.get("usage") or {}
            return Completion(
                text=(choice.get("message") or {}).get("content") or "",
                refused=refused,
                refusal_category="content_filter" if refused else None,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                model=model,
                cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            )
        raise RuntimeError(f"API unavailable after {self.retries} attempts ({last})")


def _is_transport_error(e: Exception) -> bool:
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(e, httpx.TransportError)    # RemoteProtocolError, ReadError, ConnectError, timeouts, ...


DeepSeekProvider = OpenAICompatProvider      # original name, kept for existing code and tests

CLAUDE_SYSTEM = ("You are a plain text assistant. Follow the instructions in the message exactly and reply "
                 "with only what they ask for.")

# How to drive each supported CLI as a text-completion engine (tools off / read-only, no saved sessions).
# Flags verified against `claude --help` and OpenAI's `codex exec` reference. "stdin" = prompt on stdin.
CLI_PRESETS: dict[str, dict[str, Any]] = {
    "claude": {"exe": "claude",
               "args": ["-p", "--output-format", "text", "--tools", "", "--no-session-persistence",
                        "--system-prompt", CLAUDE_SYSTEM],
               "model_flag": "--model", "output": "stdout"},
    "codex": {"exe": "codex",
              "args": ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only"],
              "model_flag": "-m", "output": "file", "outfile_flag": "-o", "prompt_arg": "-"},
}


def flatten_prompt(system: str, messages: list[dict[str, Any]]) -> str:
    """One text prompt from a system prompt and a conversation, for CLIs that take a single prompt."""
    parts = ["# Instructions (follow these exactly)", system.strip(), "", "# Conversation"]
    for m in messages:
        content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        parts += [f"## {'USER' if m['role'] == 'user' else 'ASSISTANT'}", content.strip(), ""]
    parts += ["## ASSISTANT", "(Write your reply to the last USER message now.)"]
    return "\n".join(parts)


def _default_cli_run(cmd: list[str], input_text: str, timeout: float, cwd: str | None) -> tuple[int, str, str]:
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


class CliProvider:
    """A command-line agent used as a plain text-completion backend, so people with only a CLI
    subscription (no API key) can run the whole orchestrator. Configure with a preset ('claude',
    'codex') or a custom command list:

        {type: cli, preset: codex}
        {type: cli, command: ["mytool", "--model", "{model}"], input: stdin, output: stdout}

    Custom commands may use {model} and {outfile}. Token counts are rough estimates (chars/4) because
    CLIs do not report usage. Each call spawns a process, so it is slower than an API call.
    """

    def __init__(self, preset: str | None = None, command: list[str] | None = None, input: str = "stdin",
                 output: str = "stdout", timeout: float = 900, cwd: str | None = None,
                 run: Callable[..., tuple[int, str, str]] = _default_cli_run,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        if preset is None and not command:
            raise ValueError("a CLI provider needs a preset or a command")
        if preset is not None and preset not in CLI_PRESETS:
            raise ValueError(f"unknown CLI preset {preset!r}; known: {sorted(CLI_PRESETS)}")
        self.preset, self.command, self.input, self.output = preset, command, input, output
        self.timeout, self.cwd, self.run, self.which = timeout, cwd, run, which

    @property
    def executable(self) -> str:
        return CLI_PRESETS[self.preset]["exe"] if self.preset else self.command[0]

    def build(self, model: str, outfile: str | None) -> tuple[list[str], str | None]:
        """(command, prompt_arg) - prompt_arg is appended last when the CLI takes the prompt as an argument."""
        exe = self.which(self.executable)
        if exe is None:
            raise RuntimeError(f"CLI '{self.executable}' was not found on PATH")
        use_model = model not in ("", "default")
        if self.preset:
            p = CLI_PRESETS[self.preset]
            cmd = [exe, *p["args"]]
            if p["output"] == "file":
                cmd += [p["outfile_flag"], outfile or ""]
            if use_model:
                cmd += [p["model_flag"], model]
            return cmd, p.get("prompt_arg")
        cmd = [exe] + [a.replace("{model}", model if use_model else "").replace("{outfile}", outfile or "")
                       for a in self.command[1:]]
        return cmd, None

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        prompt = flatten_prompt(system, messages)
        wants_file = (CLI_PRESETS[self.preset]["output"] == "file") if self.preset else (
            self.output == "file" or any("{outfile}" in a for a in self.command))
        out_dir = tempfile.mkdtemp(prefix="orch-cli-") if wants_file else None
        outfile = str(Path(out_dir) / "reply.txt") if out_dir else None
        try:
            cmd, prompt_arg = self.build(model, outfile)
            if self.preset:
                if prompt_arg:
                    cmd.append(prompt_arg)                      # e.g. codex: "-" means read stdin
                stdin_text = prompt
            elif self.input == "arg":
                cmd.append(prompt)
                stdin_text = ""
            else:
                stdin_text = prompt
            rc, stdout, stderr = self.run(cmd, stdin_text, self.timeout, self.cwd)
            if rc != 0:
                msg = f"{self.executable} exited with code {rc}: {(stderr or stdout).strip()[-300:]}"
                raise (ModelUnavailable if _LIMIT_RE.search(stderr or stdout or "") else RuntimeError)(msg)
            if outfile and Path(outfile).is_file():
                text = Path(outfile).read_text(encoding="utf-8", errors="replace")
            else:
                text = stdout
            text = text.strip()
            if not text:
                raise RuntimeError(f"{self.executable} returned no text")
        finally:
            if out_dir:
                shutil.rmtree(out_dir, ignore_errors=True)
        return Completion(text=text, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4, model=model)


class RoutingProvider:
    """Routes each model ID to its provider, creating providers lazily so a config that only uses one
    vendor never needs another's key. "<provider>:<model>" IDs route explicitly (used for CLIs)."""

    def __init__(self, factories: dict[str, Any], model_provider: dict[str, str],
                 default: str = "anthropic") -> None:
        self.factories, self.model_provider, self.default = factories, model_provider, default
        self._made: dict[str, Provider] = {}

    def resolve(self, model: str) -> tuple[str, str]:
        """(provider name, model id to send to that provider)."""
        if ":" in model:
            head, rest = model.split(":", 1)
            if head in self.factories:
                return head, rest
        return self.provider_name(model), model

    def provider_name(self, model: str) -> str:
        if ":" in model and model.split(":", 1)[0] in self.factories:
            return model.split(":", 1)[0]
        if model in self.model_provider:
            return self.model_provider[model]
        if model.startswith("deepseek"):
            return "deepseek"
        if model.startswith("claude"):
            return "anthropic"
        return self.default

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        name, real = self.resolve(model)
        if name not in self._made:
            if name not in self.factories:
                raise RuntimeError(f"no provider '{name}' configured for model {model}")
            self._made[name] = self.factories[name]()
        try:
            c = self._made[name].complete(model=real, system=system, messages=messages, max_tokens=max_tokens)
        except ModelUnavailable as e:
            mark_unavailable(model, str(e))
            raise
        c.model = model                       # report the ID the orchestrator uses, e.g. "codex-cli:default"
        return c


BUILTIN_KINDS = {"anthropic": "anthropic", "deepseek": "openai_compat", "openai": "openai_compat",
                 "ollama": "openai_compat", "claude-cli": "cli", "codex-cli": "cli"}


def provider_kind(name: str, spec: dict[str, Any]) -> str:
    """'anthropic', 'openai_compat' or 'cli' for a configured provider."""
    if spec.get("type"):
        return spec["type"]
    if "preset" in spec or "command" in spec:
        return "cli"
    return BUILTIN_KINDS.get(name, "openai_compat")


def build_provider(cfg: dict[str, Any]) -> RoutingProvider:
    """Routing provider from config: every entry of `providers:` becomes a backend, and per-tier
    `provider:` fields map plain model IDs to backends."""
    pcfg = cfg.get("providers", {})
    factories: dict[str, Any] = {}
    for name, spec in pcfg.items():
        kind = provider_kind(name, spec)
        if kind == "anthropic":
            factories[name] = lambda s=spec: AnthropicProvider(api_key_env=s.get("api_key_env"))
        elif kind == "cli":
            factories[name] = lambda s=spec: CliProvider(
                preset=s.get("preset"), command=s.get("command"), input=s.get("input", "stdin"),
                output=s.get("output", "stdout"), timeout=s.get("timeout", 900))
        else:
            factories[name] = lambda s=spec: OpenAICompatProvider(
                api_key_env=s.get("api_key_env", ""), base_url=s.get("base_url", "https://api.deepseek.com"),
                max_output_tokens=s.get("max_output_tokens", 32000), require_key=s.get("require_key", True))
    model_provider: dict[str, str] = {}
    models = cfg.get("models", {})
    for tier in ("top", "manager", "worker"):
        spec = models.get(tier) or {}
        if spec.get("model") and spec.get("provider"):
            model_provider[spec["model"]] = spec["provider"]
    for item in models.get("fallback_chain") or []:
        if isinstance(item, dict) and item.get("model") and item.get("provider"):
            model_provider[item["model"]] = item["provider"]
    return RoutingProvider(factories, model_provider)


def requirements(cfg: dict[str, Any], models: list[str]) -> dict[str, list[str]]:
    """What the given model IDs need that is not available: {'keys': [env vars], 'clis': [executables]}."""
    router = build_provider(cfg)
    keys: list[str] = []
    clis: list[str] = []
    for m in dict.fromkeys(models):
        name, _ = router.resolve(m)
        spec = cfg.get("providers", {}).get(name, {})
        kind = provider_kind(name, spec)
        if kind == "cli":
            exe = CLI_PRESETS[spec["preset"]]["exe"] if spec.get("preset") else (spec.get("command") or [""])[0]
            if exe and shutil.which(exe) is None and exe not in clis:
                clis.append(exe)
        elif spec.get("require_key", True):
            var = spec.get("api_key_env")
            if var and not os.environ.get(var) and var not in keys:
                keys.append(var)
    return {"keys": keys, "clis": clis}
