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
