import json

import httpx
import pytest

from orchestrator.config import load_config
from orchestrator.providers import (Completion, DeepSeekProvider, MockProvider, RoutingProvider,
                                    build_provider)


def ds(handler, **kw):
    return DeepSeekProvider(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                            backoff=0, **kw)


def ok_body(text="hi", finish="stop"):
    return {"choices": [{"message": {"content": text, "reasoning_content": "thinking..."}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


def test_deepseek_request_shape_and_response():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json=ok_body("answer"))

    c = ds(handler, max_output_tokens=8000).complete(
        model="deepseek-flash", system="sys", messages=[{"role": "user", "content": "q"}], max_tokens=16000)
    assert seen["url"] == "https://api.deepseek.com/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["model"] == "deepseek-flash" and seen["body"]["max_tokens"] == 8000   # clamped
    assert c.text == "answer" and "thinking" not in c.text            # reasoning dropped
    assert (c.input_tokens, c.output_tokens, c.model) == (11, 7, "deepseek-flash")


def test_content_filter_is_a_refusal():
    c = ds(lambda r: httpx.Response(200, json=ok_body("", "content_filter"))).complete(
        model="deepseek-v4-pro", system="s", messages=[])
    assert c.refused and c.refusal_category == "content_filter"


def test_retries_then_succeeds_and_gives_up():
    n = {"calls": 0}

    def flaky(req):
        n["calls"] += 1
        return httpx.Response(503) if n["calls"] < 3 else httpx.Response(200, json=ok_body())

    assert ds(flaky).complete(model="deepseek-flash", system="s", messages=[]).text == "hi"
    with pytest.raises(RuntimeError, match="unavailable"):
        ds(lambda r: httpx.Response(429)).complete(model="deepseek-flash", system="s", messages=[])


def test_client_errors_and_missing_key():
    with pytest.raises(RuntimeError, match="401"):
        ds(lambda r: httpx.Response(401, text="bad key")).complete(model="m", system="s", messages=[])
    p = DeepSeekProvider(api_key=None, api_key_env="NO_SUCH_ENV_VAR_XYZ")
    with pytest.raises(RuntimeError, match="NO_SUCH_ENV_VAR_XYZ"):
        p.complete(model="m", system="s", messages=[])


def test_routing_by_config_and_prefix():
    a, d = MockProvider(["from-a"]), MockProvider(["from-d", "from-d2"])
    r = RoutingProvider({"anthropic": lambda: a, "deepseek": lambda: d}, {"weird-model": "deepseek"})
    assert r.complete(model="claude-opus-5-5", system="s", messages=[]).text == "from-a"
    assert r.complete(model="deepseek-v4-pro", system="s", messages=[]).text == "from-d"
    assert r.complete(model="weird-model", system="s", messages=[]).text == "from-d2"
    assert r.provider_name("unknown-thing") == "anthropic"
    with pytest.raises(RuntimeError, match="no provider"):
        RoutingProvider({}, {}).complete(model="claude-x", system="s", messages=[])


def test_providers_created_lazily():
    made = []
    r = RoutingProvider({"anthropic": lambda: made.append("a") or MockProvider(["x"]),
                         "deepseek": lambda: made.append("d") or MockProvider(["y"])}, {})
    r.complete(model="deepseek-flash", system="s", messages=[])
    assert made == ["d"]                                  # no Anthropic client/key needed


def test_build_provider_from_mixed_config():
    cfg = load_config(None)
    cfg["models"]["top"] = {"provider": "anthropic", "model": "claude-opus-5-5"}
    cfg["models"]["manager"] = {"provider": "deepseek", "model": "deepseek-v4-pro"}
    cfg["models"]["worker"] = {"provider": "deepseek", "model": "deepseek-flash"}
    r = build_provider(cfg)
    assert [r.provider_name(m) for m in ("claude-opus-5-5", "deepseek-v4-pro", "deepseek-flash")] == \
        ["anthropic", "deepseek", "deepseek"]


def test_a_connection_dropped_mid_response_is_retried_not_fatal():
    n = {"calls": 0}

    def flaky(req):
        n["calls"] += 1
        if n["calls"] <= 2:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return httpx.Response(200, json=ok_body("recovered"))

    assert ds(flaky).complete(model="deepseek-v4-pro", system="s", messages=[]).text == "recovered"
    assert n["calls"] == 3


def test_transport_errors_that_never_clear_give_a_clear_error_and_other_errors_are_not_swallowed():
    def always_drops(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(RuntimeError, match=r"unavailable after 3 attempts \(ReadTimeout\)"):
        ds(always_drops).complete(model="m", system="s", messages=[])

    def bug(req):
        raise ValueError("a real bug must surface, not be retried")

    with pytest.raises(ValueError, match="real bug"):
        ds(bug).complete(model="m", system="s", messages=[])


# ---------------------------------------------------------------- Claude API: caching, usage, credit limits

class _FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class _FakeAnthropic:
    def __init__(self, msg=None, error=None):
        self.kwargs, self.msg, self.error = None, msg, error
        self.messages = self

    def stream(self, **kw):
        self.kwargs = kw
        if self.error:
            raise self.error
        return _FakeStream(self.msg)


def _msg(text="hi", stop="end_turn", inp=100, out=20, read=0, write=0):
    from types import SimpleNamespace as NS
    return NS(content=[NS(type="thinking", thinking="..."), NS(type="text", text=text)], stop_reason=stop,
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                       cache_creation_input_tokens=write))


def test_anthropic_requests_use_prompt_caching_and_count_the_whole_prompt():
    from orchestrator.providers import AnthropicProvider
    fake = _FakeAnthropic(_msg("answer", inp=50, out=10, read=9000, write=1000))
    c = AnthropicProvider(client=fake).complete(model="claude-haiku-5-5", system="role and protocol",
                                                messages=[{"role": "user", "content": "q"}])
    assert fake.kwargs["cache_control"] == {"type": "ephemeral"}                       # automatic tail caching
    assert fake.kwargs["system"] == [{"type": "text", "text": "role and protocol", "cache_control": {"type": "ephemeral"}}]
    assert c.text == "answer"                                                            # thinking blocks dropped
    assert (c.input_tokens, c.cache_hit_tokens, c.cache_write_tokens, c.output_tokens) == (10050, 9000, 1000, 10)


def test_anthropic_out_of_credit_is_model_unavailable_and_other_errors_pass_through():
    from orchestrator.providers import AnthropicProvider, ModelUnavailable

    class APIErr(Exception):
        def __init__(self, msg, status):
            super().__init__(msg)
            self.status_code = status

    credit = APIErr("Your credit balance is too low to access the Anthropic API.", 400)
    with pytest.raises(ModelUnavailable):
        AnthropicProvider(client=_FakeAnthropic(error=credit)).complete(model="m", system="", messages=[])
    with pytest.raises(APIErr):
        AnthropicProvider(client=_FakeAnthropic(error=APIErr("invalid request: bad field", 400))).complete(
            model="m", system="", messages=[])


def test_cache_writes_and_reads_are_priced_separately():
    from orchestrator.limits import estimate_cost
    u = {"m": {"input_tokens": 10_000_000, "cache_hit_tokens": 8_000_000, "cache_write_tokens": 1_000_000,
               "output_tokens": 1_000_000}}
    prices = {"m": {"input": 0.10, "cache_hit": 0.01, "output": 0.50}}                  # Haiku 5.5 rates
    # 1M uncached * 0.10 + 8M reads * 0.01 + 1M writes * 0.125 (1.25x input) + 1M out * 0.50
    assert estimate_cost(u, prices) == pytest.approx(0.10 + 0.08 + 0.125 + 0.50)
