"""Provider interface. Nothing else in the runtime imports a vendor SDK."""
from __future__ import annotations

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
    """Claude via the official SDK. Reads credentials from the environment
    (ANTHROPIC_API_KEY, or an `ant auth login` profile).

    Refusals are surfaced to the caller as Completion.refused. We deliberately do NOT
    use server-side refusal fallbacks: refusal handling is policy owned by the
    orchestrator (retry once with honest context, then escalate to the human).
    """

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # lazy: only needed when this provider is used
            client = anthropic.Anthropic()
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
