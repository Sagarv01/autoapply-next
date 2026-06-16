"""BillingScreen: show the current plan + launch the Stripe upgrade.

Entitlement is fetched off the GUI thread (proxy_billing.fetch_subscription_status
is async). "Upgrade to Pro" runs checkout_flow.run_checkout off-thread (it opens
the browser via QDesktopServices, waits on the loopback, polls for the tier flip)
and reflects the outcome. Drives the real runner with injected async fakes.
"""

from __future__ import annotations

import pytest

from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.billing_screen import BillingScreen


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, runner, **kw):
    kw.setdefault("open_url", lambda u: None)
    s = BillingScreen(runner=runner, **kw)
    qtbot.addWidget(s)
    return s


def test_free_plan_shows_upgrade(qtbot, runner):
    async def fetch():
        return {"tier": "free", "applications_used": 3, "applications_limit": 50, "applications_remaining": 47}

    s = _screen(qtbot, runner, fetch_status=fetch)
    with qtbot.waitSignal(s.status_loaded, timeout=3000):
        s.refresh()
    assert s.can_upgrade()
    assert "free" in s.status_text().lower()


def test_pro_plan_hides_upgrade(qtbot, runner):
    async def fetch():
        return {"tier": "pro", "status": "active"}

    s = _screen(qtbot, runner, fetch_status=fetch)
    with qtbot.waitSignal(s.status_loaded, timeout=3000):
        s.refresh()
    assert not s.can_upgrade()
    assert "pro" in s.status_text().lower()


def test_upgrade_launches_checkout_and_handles_upgraded(qtbot, runner):
    async def fetch():
        return {"tier": "free", "applications_used": 0, "applications_limit": 50}

    captured: dict = {}

    async def run_checkout(*, plan, open_url, **kw):
        captured["plan"] = plan
        captured["open_url"] = open_url
        return {"outcome": "upgraded", "status": {"tier": "pro"}}

    s = _screen(qtbot, runner, fetch_status=fetch, run_checkout=run_checkout)
    with qtbot.waitSignal(s.status_loaded, timeout=3000):
        s.refresh()
    with qtbot.waitSignal(s.checkout_done, timeout=3000):
        s.start_upgrade()
    assert captured["plan"] == "pro"
    assert callable(captured["open_url"])
    assert "pro" in s.status_text().lower()
    assert not s.can_upgrade()  # now on Pro


def test_checkout_canceled_keeps_upgrade_available(qtbot, runner):
    async def fetch():
        return {"tier": "free", "applications_used": 0, "applications_limit": 50}

    async def run_checkout(*, plan, open_url, **kw):
        return {"outcome": "canceled", "status": None}

    s = _screen(qtbot, runner, fetch_status=fetch, run_checkout=run_checkout)
    with qtbot.waitSignal(s.status_loaded, timeout=3000):
        s.refresh()
    with qtbot.waitSignal(s.checkout_done, timeout=3000):
        s.start_upgrade()
    assert s.can_upgrade()  # still free, can retry


def test_checkout_pending_tells_user_were_confirming(qtbot, runner):
    async def fetch():
        return {"tier": "free", "applications_used": 0, "applications_limit": 50}

    async def run_checkout(*, plan, open_url, **kw):
        return {"outcome": "pending", "status": {"tier": "free"}}

    s = _screen(qtbot, runner, fetch_status=fetch, run_checkout=run_checkout)
    with qtbot.waitSignal(s.status_loaded, timeout=3000):
        s.refresh()
    with qtbot.waitSignal(s.checkout_done, timeout=3000):
        s.start_upgrade()
    assert "confirm" in s.status_text().lower()
