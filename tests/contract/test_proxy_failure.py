"""TASKS 2.2: proxy-client failure handling.

- 401 -> refresh the Supabase session once and retry exactly once.
- Fatal proxy conditions (auth-expired-after-refresh, subscription inactive,
  client too old, quota exhausted, kill switch) halt the batch and surface in
  the UI via is_fatal_condition; transient ProxyUnavailableError is NOT fatal
  (the generic retry + consecutive-failure breaker handle outages).
- Kill switch: the client can poll the proxy's public /api/config.
"""

from __future__ import annotations

import pytest

from autoapply_next.engine import llm_proxy
from autoapply_next.engine.llm_adapter import _proxy_claude_complete
from autoapply_next.engine.persistence import is_fatal_condition


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _reset_providers():
    yield
    llm_proxy.set_access_token_provider(None)
    llm_proxy.set_token_refresher(None)


# ------------------------------------------------- 401 refresh-and-retry-once


async def test_401_refreshes_and_retries_once_then_succeeds(monkeypatch):
    calls = {"complete": 0, "refresh": 0}

    async def _fake_complete(*, system, user, model=None, task=None, timeout=180.0):
        calls["complete"] += 1
        if calls["complete"] == 1:
            raise llm_proxy.AuthExpiredError("expired", status=401)
        return "AFTER REFRESH"

    def _refresher():
        calls["refresh"] += 1

    monkeypatch.setattr(llm_proxy, "proxy_complete", _fake_complete)
    llm_proxy.set_token_refresher(_refresher)

    out = await _proxy_claude_complete(system="s", user="u")
    assert out == "AFTER REFRESH"
    assert calls["complete"] == 2  # original + one retry
    assert calls["refresh"] == 1  # refreshed exactly once


async def test_401_without_refresher_propagates_no_retry(monkeypatch):
    calls = {"complete": 0}

    async def _fake_complete(*, system, user, model=None, task=None, timeout=180.0):
        calls["complete"] += 1
        raise llm_proxy.AuthExpiredError("expired", status=401)

    monkeypatch.setattr(llm_proxy, "proxy_complete", _fake_complete)
    llm_proxy.set_token_refresher(None)

    with pytest.raises(llm_proxy.AuthExpiredError):
        await _proxy_claude_complete(system="s", user="u")
    assert calls["complete"] == 1  # no retry without a refresher


async def test_401_still_failing_after_refresh_propagates_once(monkeypatch):
    calls = {"complete": 0, "refresh": 0}

    async def _fake_complete(*, system, user, model=None, task=None, timeout=180.0):
        calls["complete"] += 1
        raise llm_proxy.AuthExpiredError("expired", status=401)

    def _refresher():
        calls["refresh"] += 1

    monkeypatch.setattr(llm_proxy, "proxy_complete", _fake_complete)
    llm_proxy.set_token_refresher(_refresher)

    with pytest.raises(llm_proxy.AuthExpiredError):
        await _proxy_claude_complete(system="s", user="u")
    assert calls["complete"] == 2  # tried once more after refresh
    assert calls["refresh"] == 1  # refreshed exactly once (no loop)


# ------------------------------------------------------------ kill switch


async def test_submissions_enabled_reads_config(monkeypatch):
    async def _fake_get(url, timeout):
        assert url.endswith("/api/config")
        return _FakeResp(200, {"submissions_enabled": False, "kill_switch_active": True})

    monkeypatch.setattr(llm_proxy, "_http_get", _fake_get)
    assert await llm_proxy.submissions_enabled() is False


async def test_submissions_enabled_true_when_enabled(monkeypatch):
    async def _fake_get(url, timeout):
        return _FakeResp(200, {"submissions_enabled": True, "kill_switch_active": False})

    monkeypatch.setattr(llm_proxy, "_http_get", _fake_get)
    assert await llm_proxy.submissions_enabled() is True


async def test_submissions_enabled_fails_open_on_error(monkeypatch):
    async def _fake_get(url, timeout):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(llm_proxy, "_http_get", _fake_get)
    # Fail-open: a transient config-fetch failure does not block; preflight's
    # PROXY_UNREACHABLE check (Phase 5) handles a real outage.
    assert await llm_proxy.submissions_enabled() is True


def test_killswitcherror_exists():
    assert issubclass(llm_proxy.KillSwitchError, llm_proxy.ProxyError)


# ------------------------------------------ fatal-for-batch classification


@pytest.mark.parametrize(
    "exc_name",
    ["AuthExpiredError", "SubscriptionExpiredError", "ClientTooOldError", "QuotaExceededError", "KillSwitchError"],
)
def test_proxy_fatal_conditions_halt_batch(exc_name):
    reason = is_fatal_condition(exception_type=exc_name, error_message="")
    assert reason  # non-empty reason -> batch halts + surfaces


def test_proxy_unavailable_is_not_fatal():
    # Transient: handled by retry + consecutive-failure breaker, not a hard halt.
    assert is_fatal_condition(exception_type="ProxyUnavailableError", error_message="") is None
