"""Orchestrates the Stripe Checkout round-trip for the desktop app.

run_checkout wires the loopback listener (checkout_return), the proxy billing
client (proxy_billing), and a browser opener (Qt's QDesktopServices.openUrl,
injected) into one awaitable:

  start listener -> create session with its return URLs -> open browser ->
  wait for the return -> on success, poll the proxy until the webhook flips the
  tier -> always stop the listener.

Outcomes: "upgraded" (tier is now paid), "pending" (success return but the
webhook hadn't landed within the poll window), "canceled", "timeout", "error".
Everything external is injectable so the flow is testable without Qt or a network.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from . import proxy_billing
from .checkout_return import LoopbackReturnServer

_PAID_TIERS = ("basic", "pro")


def _is_paid(sub: dict | None) -> bool:
    return (sub or {}).get("tier") in _PAID_TIERS


async def run_checkout(
    *,
    plan: str,
    open_url: Callable[[str], None],
    interval: str | None = None,
    make_server: Callable[..., LoopbackReturnServer] = LoopbackReturnServer,
    create_session: Callable[..., Awaitable[dict]] = proxy_billing.create_checkout_session,
    fetch_status: Callable[[], Awaitable[dict]] = proxy_billing.fetch_subscription_status,
    wait_timeout: float = 600.0,
    poll_attempts: int = 10,
    poll_interval: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict:
    """Drive checkout to completion. Returns {"outcome": str, "status": dict|None}."""
    server = make_server()
    server.start()
    try:
        session = await create_session(
            plan=plan,
            interval=interval,
            success_url=server.success_url,
            cancel_url=server.cancel_url,
        )
        url = (session or {}).get("url")
        if not url:
            return {"outcome": "error", "status": None}

        open_url(url)

        # wait() blocks on a threading.Event; run it off the event loop.
        returned = await asyncio.to_thread(server.wait, wait_timeout)
        if returned != "success":
            return {"outcome": "canceled" if returned == "cancel" else "timeout", "status": None}

        # The browser returned success, but the tier flip is webhook-driven and
        # may lag. Poll the proxy a bounded number of times for it to land.
        sub: dict | None = None
        for attempt in range(poll_attempts):
            sub = await fetch_status()
            if _is_paid(sub):
                return {"outcome": "upgraded", "status": sub}
            if attempt < poll_attempts - 1:
                await sleep(poll_interval)
        return {"outcome": "pending", "status": sub}
    finally:
        server.stop()
