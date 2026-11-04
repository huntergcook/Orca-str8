"""Provider interface and implementations. Nothing else in the runtime imports a vendor SDK.

  AnthropicProvider  Claude via the official SDK
  DeepSeekProvider   DeepSeek's OpenAI-format chat completions API over plain httpx
  RoutingProvider    sends each model ID to the provider that serves it
  MockProvider       scripted, for tests
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Completion:
    text: str
    refused: bool = False
    refusal_category: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


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
        # Stream so large max_tokens don't hit HTTP timeouts.
        with self.client.messages.stream(model=model, max_tokens=max_tokens,
                                         system=system, messages=messages) as stream:
            msg = stream.get_final_message()
        text = "".join(b.text for b in msg.content if b.type == "text")
        refused = msg.stop_reason == "refusal"
        details = getattr(msg, "stop_details", None)
        return Completion(
            text=text,
            refused=refused,
            refusal_category=getattr(details, "category", None) if refused else None,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            model=model,
        )


class DeepSeekProvider:
    """DeepSeek's OpenAI-format API (POST {base_url}/chat/completions).

    Both current models (deepseek-flash, deepseek-v4-pro) think by default; only the final
    `content` is returned to the orchestrator (reasoning_content is dropped). A `content_filter`
    finish is treated as a refusal so it follows the same retry-once-then-escalate policy.
    """

    def __init__(self, api_key: str | None = None, api_key_env: str = "DEEPSEEK_API_KEY",
                 base_url: str = "https://api.deepseek.com", max_output_tokens: int = 32000,
                 client: Any | None = None, retries: int = 3, backoff: float = 2.0) -> None:
        self.api_key = api_key or os.environ.get(api_key_env or "")
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.retries, self.backoff = retries, backoff
        if client is None:
            import httpx
            client = httpx.Client(timeout=600)
        self.client = client

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        if not self.api_key:
            raise RuntimeError(f"no DeepSeek API key: set the {self.api_key_env} environment variable")
        body = {"model": model, "max_tokens": min(max_tokens, self.max_output_tokens), "stream": False,
                "messages": [{"role": "system", "content": system}, *messages]}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(self.retries):
            r = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(self.backoff * (2 ** attempt))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"DeepSeek API error {r.status_code}: {r.text[:300]}")
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
            )
        raise RuntimeError(f"DeepSeek API unavailable after {self.retries} attempts ({last})")


class RoutingProvider:
    """Routes each model ID to its provider, creating providers lazily so a config that only
    uses DeepSeek never needs an Anthropic key (and vice versa)."""

    def __init__(self, factories: dict[str, Any], model_provider: dict[str, str],
                 default: str = "anthropic") -> None:
        self.factories, self.model_provider, self.default = factories, model_provider, default
        self._made: dict[str, Provider] = {}

    def provider_name(self, model: str) -> str:
        if model in self.model_provider:
            return self.model_provider[model]
        if model.startswith("deepseek"):
            return "deepseek"
        if model.startswith("claude"):
            return "anthropic"
        return self.default

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int = 16000) -> Completion:
        name = self.provider_name(model)
        if name not in self._made:
            if name not in self.factories:
                raise RuntimeError(f"no provider '{name}' configured for model {model}")
            self._made[name] = self.factories[name]()
        return self._made[name].complete(model=model, system=system, messages=messages, max_tokens=max_tokens)


def build_provider(cfg: dict[str, Any]) -> RoutingProvider:
    """Routing provider from config: `providers:` section plus per-tier `provider:` fields."""
    pcfg = cfg.get("providers", {})
    factories = {
        "anthropic": lambda: AnthropicProvider(api_key_env=pcfg.get("anthropic", {}).get("api_key_env")),
        "deepseek": lambda: DeepSeekProvider(
            api_key_env=pcfg.get("deepseek", {}).get("api_key_env", "DEEPSEEK_API_KEY"),
            base_url=pcfg.get("deepseek", {}).get("base_url", "https://api.deepseek.com"),
            max_output_tokens=pcfg.get("deepseek", {}).get("max_output_tokens", 32000)),
    }
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
