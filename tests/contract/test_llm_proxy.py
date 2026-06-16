"""TASKS 2.1: the engine's LLM chokepoint (claude_complete) routes through the
AutoApply proxy's generic /api/llm/complete endpoint instead of the claude CLI.

These are mocked unit tests of the seam: the proxy client (status mapping +
payload + headers) and the monkey-patch installer that swaps the bound
`claude_complete` symbol in matcher/tailorer/seek_apply. End-to-end against a
live proxy needs a real Supabase JWT (BLOCKED); the seam itself is verified here.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest

from autoapply_next.engine import llm_proxy
from autoapply_next.engine.llm_adapter import (
    ProxyLLM,
    _proxy_claude_complete,
    _proxy_claude_complete_tailor,
)


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def _token():
    llm_proxy.set_access_token_provider(lambda: "test-jwt")
    yield
    llm_proxy.set_access_token_provider(None)


# ----------------------------------------------------------- proxy client


async def test_proxy_complete_posts_and_returns_text(monkeypatch, _token):
    captured: dict = {}

    async def _fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return _FakeResp(200, {"text": "PROXY REPLY"})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    monkeypatch.setenv("AUTOAPPLY_PROXY_URL", "https://api.example.test")

    out = await llm_proxy.proxy_complete(system="S", user="U", model="claude-sonnet-4-6")

    assert out == "PROXY REPLY"
    assert captured["url"] == "https://api.example.test/api/llm/complete"
    assert captured["headers"]["Authorization"] == "Bearer test-jwt"
    assert captured["headers"]["X-Client-Version"]  # version header always sent (TASKS 1.7/7.5)
    assert captured["payload"] == {"system": "S", "user": "U", "model": "claude-sonnet-4-6"}


async def test_proxy_complete_includes_task_when_set(monkeypatch, _token):
    captured: dict = {}

    async def _fake_post(url, headers, payload, timeout):
        captured.update(payload)
        return _FakeResp(200, {"text": "x"})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    await llm_proxy.proxy_complete(system="s", user="u", task="tailor")
    assert captured["task"] == "tailor"


async def test_proxy_complete_omits_task_when_none(monkeypatch, _token):
    captured: dict = {}

    async def _fake_post(url, headers, payload, timeout):
        captured.update(payload)
        return _FakeResp(200, {"text": "x"})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    await llm_proxy.proxy_complete(system="s", user="u")
    assert "task" not in captured


async def test_proxy_complete_maps_403_to_needs_pro(monkeypatch, _token):
    async def _fake_post(url, headers, payload, timeout):
        return _FakeResp(403, {"detail": {"code": "needs_pro"}})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    with pytest.raises(llm_proxy.NeedsProError):
        await llm_proxy.proxy_complete(system="s", user="u", task="tailor")


async def test_proxy_complete_defaults_model_to_sonnet(monkeypatch, _token):
    captured: dict = {}

    async def _fake_post(url, headers, payload, timeout):
        captured.update(payload)
        return _FakeResp(200, {"text": "x"})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    await llm_proxy.proxy_complete(system="s", user="u")
    assert captured["model"] == llm_proxy.DEFAULT_MODEL


async def test_fetch_min_client_version(monkeypatch):
    async def _fake_get(url, timeout):
        return _FakeResp(200, {"min_client_version": "1.5.0", "submissions_enabled": True})

    monkeypatch.setattr(llm_proxy, "_http_get", _fake_get)
    assert await llm_proxy.fetch_min_client_version() == "1.5.0"


async def test_fetch_min_client_version_fails_open(monkeypatch):
    async def _fake_get(url, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm_proxy, "_http_get", _fake_get)
    assert await llm_proxy.fetch_min_client_version() == "0.0.0"


async def test_proxy_complete_without_token_raises_auth():
    llm_proxy.set_access_token_provider(None)
    with pytest.raises(llm_proxy.AuthExpiredError):
        await llm_proxy.proxy_complete(system="s", user="u")


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, llm_proxy.AuthExpiredError),
        (402, llm_proxy.SubscriptionExpiredError),
        (426, llm_proxy.ClientTooOldError),
        (429, llm_proxy.QuotaExceededError),
        (503, llm_proxy.ProxyUnavailableError),
        (500, llm_proxy.ProxyError),
    ],
)
async def test_proxy_complete_maps_status_to_exception(monkeypatch, _token, status, exc):
    async def _fake_post(url, headers, payload, timeout):
        return _FakeResp(status, {"detail": {"code": "x"}})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    with pytest.raises(exc):
        await llm_proxy.proxy_complete(system="s", user="u")


async def test_proxy_complete_network_error_is_unavailable(monkeypatch, _token):
    async def _fake_post(url, headers, payload, timeout):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)
    with pytest.raises(llm_proxy.ProxyUnavailableError):
        await llm_proxy.proxy_complete(system="s", user="u")


# -------------------------------------------- adapter monkey-patch seam


def _fake_engine_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)

    async def _orig(*, system, user, model=None, timeout=180.0):
        return "ORIGINAL-CLI"

    m.claude_complete = _orig  # type: ignore[attr-defined]
    return m


def test_proxyllm_swaps_and_restores_claude_complete(monkeypatch):
    fakes = {n: _fake_engine_module(n) for n in ("matcher", "tailorer", "seek_apply")}
    for n, m in fakes.items():
        monkeypatch.setitem(sys.modules, n, m)
    originals = {n: m.claude_complete for n, m in fakes.items()}

    adapter = ProxyLLM()
    adapter.install()
    for n, m in fakes.items():
        # tailorer routes per-job tailoring with task="tailor"; the rest generic.
        expected = _proxy_claude_complete_tailor if n == "tailorer" else _proxy_claude_complete
        assert m.claude_complete is expected
    adapter.uninstall()
    for n, m in fakes.items():
        assert m.claude_complete is originals[n]


def test_proxyllm_skips_modules_without_claude_complete(monkeypatch):
    bare = types.ModuleType("matcher")  # no claude_complete attr
    monkeypatch.setitem(sys.modules, "matcher", bare)
    with ProxyLLM(modules=("matcher",)):
        assert not hasattr(bare, "claude_complete")  # untouched, no crash


async def test_tailorer_seam_sends_tailor_task(monkeypatch, _token):
    fakes = {n: _fake_engine_module(n) for n in ("matcher", "tailorer", "seek_apply")}
    for n, m in fakes.items():
        monkeypatch.setitem(sys.modules, n, m)

    seen: dict = {}

    async def _fake_complete(*, system, user, model=None, task=None, timeout=180.0):
        seen[system] = task
        return "OK"

    monkeypatch.setattr(llm_proxy, "proxy_complete", _fake_complete)

    with ProxyLLM(modules=("matcher", "tailorer", "seek_apply")):
        await sys.modules["tailorer"].claude_complete(system="TAILOR", user="u")
        await sys.modules["matcher"].claude_complete(system="MATCH", user="u")
        await sys.modules["seek_apply"].claude_complete(system="SCREEN", user="u")

    assert seen["TAILOR"] == "tailor"   # tailoring is Pro-gated server-side
    assert seen["MATCH"] is None        # scoring is generic, open to all tiers
    assert seen["SCREEN"] is None       # screening answers are generic too


async def test_patched_seam_routes_to_proxy(monkeypatch, _token):
    fake = _fake_engine_module("matcher")
    monkeypatch.setitem(sys.modules, "matcher", fake)

    seen: dict = {}

    async def _fake_complete(*, system, user, model=None, task=None, timeout=180.0):
        seen["args"] = (system, user, model)
        return "ROUTED"

    monkeypatch.setattr(llm_proxy, "proxy_complete", _fake_complete)

    with ProxyLLM(modules=("matcher",)):
        out = await sys.modules["matcher"].claude_complete(
            system="S", user="U", model="claude-sonnet-4-6"
        )
    assert out == "ROUTED"
    assert seen["args"] == ("S", "U", "claude-sonnet-4-6")
    # restored after context exit
    assert (
        await sys.modules["matcher"].claude_complete(system="s", user="u") == "ORIGINAL-CLI"
    )
