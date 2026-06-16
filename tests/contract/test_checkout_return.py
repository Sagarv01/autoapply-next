"""Phase E: the Stripe checkout return is captured by a localhost loopback.

The desktop app can't receive a custom-scheme deep link reliably across mac/win,
so checkout opens in the system browser (QDesktopServices) with Stripe's
success_url/cancel_url pointed at a short-lived HTTP listener on a random
127.0.0.1 port. When the browser lands there, the app learns the outcome and
refreshes entitlement (fallback: poll Supabase tier on focus).
"""

from __future__ import annotations

import urllib.request

from autoapply_next.billing.checkout_return import LoopbackReturnServer


def _hit(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_success_url_hit_captures_success():
    server = LoopbackReturnServer()
    base = server.start()
    try:
        assert base.startswith("http://127.0.0.1:")
        status_code, body = _hit(server.success_url)
        assert status_code == 200
        assert "AutoApply" in body  # friendly return page, not a raw 200
        assert server.wait(timeout=5) == "success"
    finally:
        server.stop()


def test_cancel_url_hit_captures_cancel():
    server = LoopbackReturnServer()
    server.start()
    try:
        _hit(server.cancel_url)
        assert server.wait(timeout=5) == "cancel"
    finally:
        server.stop()


def test_callback_invoked_on_return():
    seen: list[str] = []
    server = LoopbackReturnServer(on_return=seen.append)
    server.start()
    try:
        _hit(server.success_url)
        server.wait(timeout=5)
        assert seen == ["success"]
    finally:
        server.stop()


def test_wait_times_out_without_a_hit():
    server = LoopbackReturnServer()
    server.start()
    try:
        assert server.wait(timeout=0.2) is None
    finally:
        server.stop()


def test_urls_share_loopback_port_and_differ():
    server = LoopbackReturnServer()
    server.start()
    try:
        port = server.port
        assert f"127.0.0.1:{port}" in server.success_url
        assert f"127.0.0.1:{port}" in server.cancel_url
        assert server.success_url != server.cancel_url
    finally:
        server.stop()


def test_first_status_wins_on_repeated_hits():
    server = LoopbackReturnServer()
    server.start()
    try:
        _hit(server.success_url)
        _hit(server.cancel_url)  # a stray later hit must not overwrite the result
        assert server.wait(timeout=5) == "success"
    finally:
        server.stop()
