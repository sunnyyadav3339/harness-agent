import json
import time

import httpx
import pytest

from harness.llm.router import AllModelsExhausted, Router


def _cfg(vision_rpm=10):
    return {
        "providers": {
            "a": {"base_url": "https://a.test/v1", "keys_env": "TEST_A_KEYS"},
            "b": {"base_url": "https://b.test/v1", "keys_env": "TEST_B_KEYS"},
        },
        "groups": {
            "vision": [{"provider": "a", "model": "vis-1", "vision": True, "rpm": vision_rpm}],
            "fast": [{"provider": "b", "model": "txt-1", "rpm": 30, "tpm": 100000}],
        },
        "fallbacks": {"vision": ["fast"], "fast": ["vision"]},
    }


def _ok(text="{\"ok\": true}"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("TEST_A_KEYS", "ka1,ka2")
    monkeypatch.setenv("TEST_B_KEYS", "kb1")


def _router(handler, cfg=None):
    r = Router(config=cfg or _cfg())
    r._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return r


async def test_429_cools_one_key_and_uses_the_other(keys):
    seen = []

    def handler(req: httpx.Request):
        key = req.headers["authorization"].split()[-1]
        seen.append(key)
        if key == "ka1":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate"})
        return _ok()

    r = _router(handler)
    for _ in range(3):
        resp = await r.complete("vision", [{"role": "user", "content": "hi"}])
        assert resp.deployment.endswith("#2")
    # ka1 was tried at most once, then sat out its cooldown
    assert seen.count("ka1") <= 1
    dep = r.deployments["vision:a/vis-1#1"]
    assert dep.cooldown_until > time.time() + 20


async def test_falls_back_to_text_group_and_strips_images(keys):
    bodies = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        bodies[req.url.host] = body
        if req.url.host == "a.test":
            return httpx.Response(503)
        return _ok()

    r = _router(handler)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]}]
    resp = await r.complete("vision", msgs, primary_wait_s=0)
    assert resp.images_stripped is True
    assert isinstance(bodies["b.test"]["messages"][0]["content"], str)
    assert "screenshot omitted" in bodies["b.test"]["messages"][0]["content"]


async def test_unsupported_json_mode_is_dropped_and_remembered(keys):
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return _ok()

    r = _router(handler)
    await r.complete("fast", [{"role": "user", "content": "x"}])
    await r.complete("fast", [{"role": "user", "content": "x"}])
    assert calls == [True, False, False]


async def test_404_disables_deployment_and_exhaustion_raises(keys):
    def handler(req):
        return httpx.Response(404, json={"error": "no such model"})

    r = _router(handler)
    with pytest.raises(AllModelsExhausted):
        await r.complete("fast", [{"role": "user", "content": "x"}], deadline_s=5)
    assert all(d.disabled for d in r.deployments.values())


async def test_client_side_rpm_throttle(keys, monkeypatch):
    monkeypatch.setenv("TEST_A_KEYS", "only")
    r = _router(lambda req: _ok(), _cfg(vision_rpm=2))
    dep = next(iter(r.deployments.values()))
    for _ in range(2):
        await r.complete("vision", [{"role": "user", "content": "x"}])
    now = time.time()
    assert dep.available_at(now, 10) > now + 50  # third call must wait for the window to slide
