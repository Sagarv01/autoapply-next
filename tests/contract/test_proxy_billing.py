"""Phase E: the desktop billing client talks to the proxy subscription API.

create_checkout_session POSTs plan + the loopback return URLs (so Stripe sends
the browser back to the app); fetch_subscription_status GETs the current tier so
the app can refresh entitlement after checkout. Both carry the Supabase JWT and
map proxy errors to the shared typed exceptions.
"""

from __future__ import annotations

import httpx
import pytest

from autoapply_next.billing import proxy_billing
from autoapply_next.engine import llm_proxy


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def _token():
    llm_proxy.set_access_token_provider(lambda: "jwt")
    yield
    llm_proxy.set_access_token_provider(None)


async def test_create_checkout_posts_plan_and_loopback_urls(monkeypatch, _token):
    captured: dict = {}

    async def _fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return _FakeResp(200, {"url": "https://checkout.stripe.test/s", "session_id": "cs_1"})

    monkeypatch.setattr(proxy_billing, "_post", _fake_post)
    monkeypatch.setenv("AUTOAPPLY_PROXY_URL", "https://api.example.test")

    out = await proxy_billing.create_checkout_session(
        plan="pro",
        interval="monthly",
        success_url="http://127.0.0.1:5/return?status=success",
        cancel_url="http://127.0.0.1:5/return?status=cancel",
    )

    assert out["url"] == "https://checkout.stripe.test/s"
    assert captured["url"] == "https://api.example.test/api/subscription/create-checkout"
    assert captured["headers"]["Authorization"] == "Bearer jwt"
    assert captured["headers"]["X-Client-Version"]
    assert captured["payload"]["plan"] == "pro"
    assert captured["payload"]["interval"] == "monthly"
    assert captured["payload"]["success_url"] == "http://127.0.0.1:5/return?status=success"
    assert captured["payload"]["cancel_url"] == "http://127.0.0.1:5/return?status=cancel"


async def test_create_checkout_without_token_raises_auth():
    llm_proxy.set_access_token_provider(None)
    with pytest.raises(llm_proxy.AuthExpiredError):
        await proxy_billing.create_checkout_session(plan="pro")


async def test_fetch_subscription_status_returns_tier(monkeypatch, _token):
    async def _fake_get(url, headers, timeout):
        assert url.endswith("/api/subscription/status")
        assert headers["Authorization"] == "Bearer jwt"
        return _FakeResp(200, {"tier": "pro", "status": "active", "applications_used": 3})

    monkeypatch.setattr(proxy_billing, "_get", _fake_get)
    out = await proxy_billing.fetch_subscription_status()
    assert out["tier"] == "pro"


async def test_status_network_error_is_unavailable(monkeypatch, _token):
    async def _fake_get(url, headers, timeout):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(proxy_billing, "_get", _fake_get)
    with pytest.raises(llm_proxy.ProxyUnavailableError):
        await proxy_billing.fetch_subscription_status()


async def test_checkout_maps_401_to_auth_expired(monkeypatch, _token):
    async def _fake_post(url, headers, payload, timeout):
        return _FakeResp(401, {"detail": {"code": "missing_token"}})

    monkeypatch.setattr(proxy_billing, "_post", _fake_post)
    with pytest.raises(llm_proxy.AuthExpiredError):
        await proxy_billing.create_checkout_session(plan="pro")
