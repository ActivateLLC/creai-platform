"""The model layer: format translation, fallback, pricing, and image generation."""

import json
import os

import httpx
import pytest

os.environ.setdefault("ENV", "development")

from app.core.config import settings                 # noqa: E402
from app.services import billing, images, models     # noqa: E402

pytestmark = pytest.mark.asyncio

THREAD = [
    {"role": "user", "content": "Make my headline punchier"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "On it."},
        {"type": "tool_use", "id": "tu_1", "name": "update_site", "input": {"headline": "Spotless, at your door"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": json.dumps({"ok": True})}]},
]
TOOLS = [{"name": "update_site", "description": "Change the site",
          "input_schema": {"type": "object", "properties": {"headline": {"type": "string"}}}}]


_REAL_CLIENT = httpx.AsyncClient


def patch_http(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture(autouse=True)
def keys():
    old = (settings.anthropic_key, settings.meta_api_key, settings.hf_token)
    object.__setattr__(settings, "anthropic_key", "ak")
    object.__setattr__(settings, "meta_api_key", "mk")
    object.__setattr__(settings, "hf_token", "hf_test")
    yield
    for k, v in zip(("anthropic_key", "meta_api_key", "hf_token"), old):
        object.__setattr__(settings, k, v)


def test_thread_translates_to_chat_completions():
    msgs, fns = models.to_openai(THREAD, TOOLS, "You are Creai")
    assert msgs[0] == {"role": "system", "content": "You are Creai"}
    assert msgs[1] == {"role": "user", "content": "Make my headline punchier"}
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "On it."
    call = msgs[2]["tool_calls"][0]
    assert call["id"] == "tu_1" and json.loads(call["function"]["arguments"]) == {"headline": "Spotless, at your door"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "tu_1", "content": '{"ok": true}'}
    assert fns[0]["function"]["name"] == "update_site"
    assert fns[0]["function"]["parameters"]["properties"]["headline"]["type"] == "string"


def test_chat_completions_response_translates_back():
    m = models.get("muse-spark-1.1")
    out = models.from_openai({
        "choices": [{"message": {"content": "Done", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "update_site", "arguments": '{"headline":"Hi"}'}},
            {"id": "c2", "type": "function", "function": {"name": "save_answer", "arguments": "not json"}}]}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "prompt_tokens_details": {"cached_tokens": 1000}}}, m)
    assert out["model"] == "muse-spark-1.1"
    assert out["content"][0] == {"type": "text", "text": "Done"}
    assert out["content"][1] == {"type": "tool_use", "id": "c1", "name": "update_site", "input": {"headline": "Hi"}}
    assert out["content"][2]["input"] == {}                          # unparseable args never crash the loop
    assert out["usage"]["input_tokens"] == 1200 and out["usage"]["output_tokens"] == 80


async def test_meta_is_called_openai_style(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    patch_http(monkeypatch, handler)
    out = await models.complete("muse-spark-1.1", THREAD, TOOLS, "sys")
    assert seen["url"] == "https://api.meta.ai/v1/chat/completions" and seen["auth"] == "Bearer mk"
    assert seen["body"]["model"] == "muse-spark-1.1" and seen["body"]["tools"][0]["type"] == "function"
    assert out["content"] == [{"type": "text", "text": "Hello"}] and out["model"] == "muse-spark-1.1"


async def test_outage_falls_back_but_bad_requests_do_not(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "api.meta.ai":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"model": "claude-sonnet-5", "content": [{"type": "text", "text": "ok"}],
                                         "usage": {"input_tokens": 5, "output_tokens": 1}})
    patch_http(monkeypatch, handler)
    out = await models.complete("muse-spark-1.1", THREAD, TOOLS, "sys")
    assert calls == ["api.meta.ai", "api.anthropic.com"] and out["model"] == "claude-sonnet-5"

    patch_http(monkeypatch, lambda req: httpx.Response(400, text="bad tool schema"))
    with pytest.raises(models.ModelRejected):
        await models.complete("muse-spark-1.1", THREAD, TOOLS, "sys")


async def test_unconfigured_provider_is_skipped(monkeypatch):
    object.__setattr__(settings, "meta_api_key", "")
    hosts = []
    patch_http(monkeypatch, lambda req: hosts.append(req.url.host) or httpx.Response(
        200, json={"content": [], "usage": {}}))
    out = await models.complete("muse-spark-1.1", THREAD, TOOLS, "sys")
    assert hosts == ["api.anthropic.com"] and out["model"] == "claude-sonnet-5"
    object.__setattr__(settings, "anthropic_key", "")
    with pytest.raises(models.ModelUnavailable):
        await models.complete("claude-fable-5-1", THREAD, TOOLS, "sys")


def test_billing_prices_every_model_and_media():
    muse = billing.usage_cost("muse-spark-1.1", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert muse == pytest.approx(1.25 + 4.25)
    fable = billing.usage_cost("claude-fable-5-1", {"input_tokens": 1_000_000})
    assert fable == pytest.approx(10.0)
    assert billing.usage_cost("image:Tongyi-MAI/Z-Image-Turbo", {"images": 3}) == pytest.approx(0.03)
    assert billing.usage_cost("mystery-model", {"output_tokens": 1_000_000}) >= 50.0   # never free
    credits, cost = billing.credits_for([("claude-sonnet-5", {"input_tokens": 3000, "output_tokens": 500}),
                                         ("image:Tongyi-MAI/Z-Image-Turbo", {"images": 2})])
    assert cost == pytest.approx(0.011 + 0.02) and credits == 10


class FakeHF:
    def __init__(self, nsfw=False):
        self.polls, self.nsfw, self.bodies = 0, nsfw, []

    def __call__(self, req):
        assert req.headers["authorization"] == "Bearer hf_test"
        u = str(req.url)
        if req.url.host == "huggingface.co":
            return httpx.Response(200, json={"inferenceProviderMapping": {
                "fal-ai": {"status": "live", "providerId": "fal-ai/z-image/turbo", "task": "text-to-image"}}})
        if req.method == "POST":
            assert u == "https://router.huggingface.co/fal-ai/fal-ai/z-image/turbo?_subdomain=queue"
            self.bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"request_id": "r1", "status": "IN_QUEUE",
                                             "response_url": "https://queue.fal.run/fal-ai/z-image/requests/r1"})
        if u.endswith("/status?_subdomain=queue"):
            self.polls += 1
            return httpx.Response(200, json={"status": "COMPLETED" if self.polls > 1 else "IN_PROGRESS"})
        assert u == "https://router.huggingface.co/fal-ai/fal-ai/z-image/requests/r1?_subdomain=queue"
        return httpx.Response(200, json={"images": [{"url": "https://v3.fal.media/files/abc.jpeg"}],
                                         "has_nsfw_concepts": [self.nsfw]})


async def test_image_generation_follows_the_queue(monkeypatch):
    monkeypatch.setattr(images, "POLL_SECONDS", 0)
    images._mapping.clear()
    hf = FakeHF()
    patch_http(monkeypatch, hf)
    url = await images.generate("A clean black SUV in a driveway at golden hour", "portrait")
    assert url == "https://v3.fal.media/files/abc.jpeg" and hf.polls == 2
    assert hf.bodies[0]["image_size"] == "portrait_4_3" and hf.bodies[0]["enable_safety_checker"] is True

    patch_http(monkeypatch, FakeHF(nsfw=True))
    with pytest.raises(images.ImageError):
        await images.generate("anything")
    object.__setattr__(settings, "hf_token", "")
    with pytest.raises(images.ImageError):
        await images.generate("anything")


async def test_eval_harness_runs_and_scores(monkeypatch):
    from app.services import agent
    from scripts import eval_models

    async def lazy(messages, tools, system, model, max_tokens=2048):
        return {"model": model, "content": [{"type": "text", "text": "Sure, tell me more about it first."}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}
    monkeypatch.setattr(agent, "_call", lazy)
    report = await eval_models.evaluate("claude-sonnet-5", runs=1)
    assert len(report["rows"]) == len(eval_models.SCENARIOS)
    assert 0 < report["score"] < 0.6              # a model that never uses tools must not pass
    assert report["fell_back"] is False and report["cost_usd"] > 0
