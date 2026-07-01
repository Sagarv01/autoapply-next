"""HTTP client that brokers the engine's LLM calls through the AutoApply proxy.

The desktop app never calls Anthropic/OpenAI directly. The vendored engine's
single LLM chokepoint, `claude_cli.claude_complete(*, system, user, model)`, is
routed here (see `llm_adapter.ProxyLLM`) to POST the proxy's generic
`/api/llm/complete` endpoint with the authenticated user's Supabase JWT. The
proxy owns the API key, model routing, per-task token caps, metering, and quota
(decision D1).

This module mirrors the exception hierarchy of the backend reference client
(autoapply.com.au/backend/llm/proxy_client.py) so failure handling (TASKS 2.2)
can branch on the same types.
"""
from __future__ import annotations

import inspect
import os
from typing import Any, Callable

import httpx

from autoapply_next.config import proxy_base_url as _config_proxy_base_url

# Mirrors the vendored claude_cli.DEFAULT_MODEL so call sites that pass
# model=DEFAULT_MODEL keep working; the proxy allowlists this model.
DEFAULT_MODEL = "claude-sonnet-4-6"


# ── exceptions (mirror backend/llm/proxy_client.py) ─────────────────────────
class ProxyError(RuntimeError):
    """Base class for proxy HTTP failures."""

    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthExpiredError(ProxyError):
    """HTTP 401: the Supabase JWT is missing, invalid, or expired."""


class SubscriptionExpiredError(ProxyError):
    """HTTP 402: the user's subscription is inactive."""


class NeedsProError(ProxyError):
    """HTTP 403 needs_pro: per-job tailoring requires the Pro tier. The current
    plan (free/basic) may apply with base documents but cannot tailor."""


class ClientTooOldError(ProxyError):
    """HTTP 426: the client is below the proxy's minimum version floor."""


class QuotaExceededError(ProxyError):
    """HTTP 429: rate limit or daily token/application quota exhausted."""


class ProxyUnavailableError(ProxyError):
    """HTTP 502/503/504 or a network/transport error. Transient, retryable."""


class KillSwitchError(ProxyError):
    """Submissions are globally disabled by the proxy kill switch (/api/config)."""


# ── configuration ───────────────────────────────────────────────────────────
def _proxy_base_url() -> str:
    # Proxy runs on GCP Cloud Run (Sydney). The default is set in
    # autoapply_next.config so it can be overridden via AUTOAPPLY_PROXY_URL.
    return _config_proxy_base_url()


def _client_version() -> str:
    """The client version sent on every proxy call so the proxy's min-version
    floor (TASKS 1.7) can reject stale clients. Single source: the package
    version, overridable via AUTOAPPLY_CLIENT_VERSION."""
    v = (os.environ.get("AUTOAPPLY_CLIENT_VERSION") or "").strip()
    if v:
        return v
    try:
        from autoapply_next import __version__  # type: ignore

        return str(__version__)
    except Exception:
        return "0.0.0"


# Access-token provider. Phase 3 auth wires this to return the live Supabase
# access token; until then it is None and proxy calls raise AuthExpiredError.
_token_provider: Callable[[], str | None] | None = None


def set_access_token_provider(provider: Callable[[], str | None] | None) -> None:
    global _token_provider
    _token_provider = provider


def _get_access_token() -> str | None:
    if _token_provider is None:
        return None
    try:
        return _token_provider()
    except Exception:
        return None


# Token refresher. Phase 3 auth wires this to silently exchange the refresh
# token for a fresh access token. Used for the 401 refresh-and-retry-once
# policy (TASKS 2.2). May be sync or async.
_token_refresher: Callable[[], Any] | None = None


def set_token_refresher(refresher: Callable[[], Any] | None) -> None:
    global _token_refresher
    _token_refresher = refresher


async def refresh_access_token() -> bool:
    """Invoke the configured refresher once. Returns True if a refresher ran
    (so the caller may retry), False if none is configured."""
    if _token_refresher is None:
        return False
    result = _token_refresher()
    if inspect.isawaitable(result):
        await result
    return True


# ── transport (factored so unit tests can stub it) ──────────────────────────
async def _http_post(url: str, headers: dict, payload: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, headers=headers, json=payload)


async def _http_get(url: str, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(url)


async def submissions_enabled(timeout: float = 10.0) -> bool:
    """Poll the proxy's public /api/config global kill switch (TASKS 2.2).

    Fail-open (returns True) if the config cannot be fetched: a real outage is
    caught by the preflight PROXY_UNREACHABLE check (Phase 5), and we do not
    want a transient config blip to silently freeze a signed-in user.
    """
    url = f"{_proxy_base_url()}/api/config"
    try:
        resp = await _http_get(url, timeout)
        if resp.status_code >= 400:
            return True
        data = resp.json() or {}
    except Exception:
        return True
    return bool(data.get("submissions_enabled", True))


async def fetch_min_client_version(timeout: float = 10.0) -> str:
    """Read the proxy's published minimum client version (/api/config). Returns
    '0.0.0' (which never forces an update) if it can't be fetched, so a config
    blip never locks a user out."""
    url = f"{_proxy_base_url()}/api/config"
    try:
        resp = await _http_get(url, timeout)
        if resp.status_code >= 400:
            return "0.0.0"
        data = resp.json() or {}
    except Exception:
        return "0.0.0"
    return str(data.get("min_client_version") or "0.0.0")


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _auth_headers() -> dict:
    """Standard authenticated proxy headers. Raises AuthExpiredError if no token
    is available (not signed in)."""
    token = _get_access_token()
    if not token:
        raise AuthExpiredError("No access token; sign in to continue.", status=401)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Client-Version": _client_version(),
    }


def _raise_for_proxy_status(resp: httpx.Response) -> None:
    """Map a proxy HTTP error status to a typed ProxyError. No-op on success.

    Shared by every proxy call (completion + billing) so failure handling
    (TASKS 2.2) branches on the same types everywhere.
    """
    sc = resp.status_code
    if sc == 401:
        raise AuthExpiredError("Session expired", status=401, detail=_safe_json(resp))
    if sc == 402:
        raise SubscriptionExpiredError("Subscription inactive", status=402, detail=_safe_json(resp))
    if sc == 403:
        raise NeedsProError("Per-job tailoring requires Pro", status=403, detail=_safe_json(resp))
    if sc == 426:
        raise ClientTooOldError("Client too old; update required", status=426, detail=_safe_json(resp))
    if sc == 429:
        raise QuotaExceededError("Usage limit reached", status=429, detail=_safe_json(resp))
    if sc in (502, 503, 504):
        raise ProxyUnavailableError(f"Proxy returned {sc}", status=sc, detail=_safe_json(resp))
    if sc >= 400:
        raise ProxyError(f"Proxy error {sc}", status=sc, detail=_safe_json(resp))


async def proxy_complete(
    *,
    system: str,
    user: str,
    model: str | None = None,
    task: str | None = None,
    timeout: float = 180.0,
) -> str:
    """Route one completion through the proxy and return the reply text.

    Same call shape as the vendored `claude_complete` (plus an optional `task`
    hint), so it can stand in for it via monkey-patch. `task="tailor"` flags
    per-job document tailoring, which the proxy gates to the Pro tier. Raises a
    typed ProxyError subclass on failure.
    """
    headers = _auth_headers()
    url = f"{_proxy_base_url()}/api/llm/complete"
    payload = {"system": system, "user": user, "model": model or DEFAULT_MODEL}
    if task:
        payload["task"] = task

    try:
        resp = await _http_post(url, headers, payload, timeout)
    except httpx.RequestError as e:
        raise ProxyUnavailableError(f"Proxy unavailable: {e}", status=503) from e

    _raise_for_proxy_status(resp)
    return str((resp.json() or {}).get("text", ""))
