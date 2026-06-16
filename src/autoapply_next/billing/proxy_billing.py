"""Desktop billing client for the proxy's subscription API.

Two calls back the paywall:
  - create_checkout_session: POST /api/subscription/create-checkout with the plan
    and the app's 127.0.0.1 loopback return URLs, returns {url, session_id}.
  - fetch_subscription_status: GET /api/subscription/status, returns the current
    tier so the app can refresh entitlement after checkout (or on focus).

Auth, base URL, client-version header, and error typing are shared with the LLM
seam (llm_proxy), so the whole app fails the same way on the same proxy errors.
"""
from __future__ import annotations

import httpx

from autoapply_next.engine import llm_proxy


# Thin transport wrappers (factored so unit tests can stub them).
async def _post(url: str, headers: dict, payload: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, headers=headers, json=payload)


async def _get(url: str, headers: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(url, headers=headers)


async def create_checkout_session(
    *,
    plan: str,
    interval: str | None = None,
    success_url: str | None = None,
    cancel_url: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Create a Stripe Checkout session and return {url, session_id}."""
    headers = llm_proxy._auth_headers()
    url = f"{llm_proxy._proxy_base_url()}/api/subscription/create-checkout"
    payload: dict = {"plan": plan}
    if interval:
        payload["interval"] = interval
    if success_url:
        payload["success_url"] = success_url
    if cancel_url:
        payload["cancel_url"] = cancel_url

    try:
        resp = await _post(url, headers, payload, timeout)
    except httpx.RequestError as e:
        raise llm_proxy.ProxyUnavailableError(f"Proxy unavailable: {e}", status=503) from e

    llm_proxy._raise_for_proxy_status(resp)
    return resp.json() or {}


async def fetch_subscription_status(*, timeout: float = 15.0) -> dict:
    """Return the user's current subscription/entitlement snapshot from the proxy."""
    headers = llm_proxy._auth_headers()
    url = f"{llm_proxy._proxy_base_url()}/api/subscription/status"

    try:
        resp = await _get(url, headers, timeout)
    except httpx.RequestError as e:
        raise llm_proxy.ProxyUnavailableError(f"Proxy unavailable: {e}", status=503) from e

    llm_proxy._raise_for_proxy_status(resp)
    return resp.json() or {}
