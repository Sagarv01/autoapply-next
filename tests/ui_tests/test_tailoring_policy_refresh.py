"""The worker sets the per-batch tailoring policy from the user's tier.

Pro -> tailoring allowed; Free/Basic -> base docs. A tier-fetch failure leaves the
policy unchanged (the proxy Pro-gate is still the real enforcement).
"""

from __future__ import annotations

import pytest

from autoapply_next.engine import tailoring_policy as tp
from autoapply_next.engine import worker as wm


@pytest.fixture(autouse=True)
def _reset():
    tp.set_tailoring_allowed(True)
    yield
    tp.set_tailoring_allowed(True)


async def test_sets_policy_from_tier(qtbot, tmp_path, monkeypatch):
    w = wm.EngineWorker(engine_workdir=tmp_path)
    try:
        async def pro():
            return {"tier": "pro"}

        monkeypatch.setattr(wm.proxy_billing, "fetch_subscription_status", pro)
        tp.set_tailoring_allowed(False)
        await w._refresh_tailoring_policy()
        assert tp.tailoring_allowed() is True

        async def free():
            return {"tier": "free"}

        monkeypatch.setattr(wm.proxy_billing, "fetch_subscription_status", free)
        await w._refresh_tailoring_policy()
        assert tp.tailoring_allowed() is False
    finally:
        w.stop_loop()


async def test_leaves_policy_unchanged_on_fetch_failure(qtbot, tmp_path, monkeypatch):
    w = wm.EngineWorker(engine_workdir=tmp_path)
    try:
        async def boom():
            raise RuntimeError("network down")

        monkeypatch.setattr(wm.proxy_billing, "fetch_subscription_status", boom)
        tp.set_tailoring_allowed(True)
        await w._refresh_tailoring_policy()
        assert tp.tailoring_allowed() is True
    finally:
        w.stop_loop()
