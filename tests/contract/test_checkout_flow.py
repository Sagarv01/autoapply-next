"""Phase E: the checkout orchestrator ties the paywall together.

run_checkout: start the loopback listener, create the Stripe session with its
return URLs, open the browser (injected), wait for the return, then poll the
proxy until the webhook-driven tier flip lands. The listener is always stopped,
even when session creation fails. Qt-agnostic (open_url is injected).
"""

from __future__ import annotations

import pytest

from autoapply_next.billing import checkout_flow
from autoapply_next.engine import llm_proxy


class _FakeServer:
    def __init__(self, wait_status):
        self._wait_status = wait_status
        self.started = False
        self.stopped = False
        self.port = 5555

    def start(self):
        self.started = True
        return "http://127.0.0.1:5555"

    @property
    def success_url(self):
        return "http://127.0.0.1:5555/return?status=success"

    @property
    def cancel_url(self):
        return "http://127.0.0.1:5555/return?status=cancel"

    def wait(self, timeout=None):
        return self._wait_status

    def stop(self):
        self.stopped = True


async def _noop_sleep(_):
    return None


async def test_successful_upgrade_polls_until_tier_flips(monkeypatch):
    server = _FakeServer("success")
    opened: list[str] = []
    created: dict = {}

    async def _create(**kw):
        created.update(kw)
        return {"url": "https://checkout.stripe.test/s", "session_id": "cs_1"}

    statuses = iter([{"tier": "free"}, {"tier": "pro", "status": "active"}])

    async def _status():
        return next(statuses)

    result = await checkout_flow.run_checkout(
        plan="pro",
        open_url=opened.append,
        make_server=lambda *a, **k: server,
        create_session=_create,
        fetch_status=_status,
        poll_attempts=5,
        poll_interval=0,
        sleep=_noop_sleep,
    )

    assert result["outcome"] == "upgraded"
    assert result["status"]["tier"] == "pro"
    assert opened == ["https://checkout.stripe.test/s"]
    assert created["plan"] == "pro"
    assert created["success_url"] == server.success_url
    assert created["cancel_url"] == server.cancel_url
    assert server.started and server.stopped


async def test_cancel_return_does_not_poll_status():
    server = _FakeServer("cancel")
    polled = {"n": 0}

    async def _create(**kw):
        return {"url": "https://checkout.stripe.test/s"}

    async def _status():
        polled["n"] += 1
        return {"tier": "free"}

    result = await checkout_flow.run_checkout(
        plan="pro",
        open_url=lambda _u: None,
        make_server=lambda *a, **k: server,
        create_session=_create,
        fetch_status=_status,
        sleep=_noop_sleep,
    )
    assert result["outcome"] == "canceled"
    assert polled["n"] == 0
    assert server.stopped


async def test_timeout_when_no_return():
    server = _FakeServer(None)

    async def _create(**kw):
        return {"url": "https://checkout.stripe.test/s"}

    async def _status():
        return {"tier": "free"}

    result = await checkout_flow.run_checkout(
        plan="pro",
        open_url=lambda _u: None,
        make_server=lambda *a, **k: server,
        create_session=_create,
        fetch_status=_status,
        sleep=_noop_sleep,
    )
    assert result["outcome"] == "timeout"
    assert server.stopped


async def test_pending_when_tier_not_yet_flipped():
    server = _FakeServer("success")

    async def _create(**kw):
        return {"url": "https://checkout.stripe.test/s"}

    async def _status():
        return {"tier": "free"}  # webhook hasn't landed within the poll window

    result = await checkout_flow.run_checkout(
        plan="pro",
        open_url=lambda _u: None,
        make_server=lambda *a, **k: server,
        create_session=_create,
        fetch_status=_status,
        poll_attempts=3,
        poll_interval=0,
        sleep=_noop_sleep,
    )
    assert result["outcome"] == "pending"
    assert server.stopped


async def test_listener_stopped_even_if_session_creation_fails():
    server = _FakeServer("success")

    async def _create(**kw):
        raise llm_proxy.ProxyUnavailableError("down", status=503)

    with pytest.raises(llm_proxy.ProxyUnavailableError):
        await checkout_flow.run_checkout(
            plan="pro",
            open_url=lambda _u: None,
            make_server=lambda *a, **k: server,
            create_session=_create,
            fetch_status=lambda: None,
            sleep=_noop_sleep,
        )
    assert server.started and server.stopped
