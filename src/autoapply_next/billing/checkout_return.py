"""A short-lived localhost HTTP listener that captures the Stripe Checkout return.

PySide6 has no reliable cross-platform custom-scheme deep link, so checkout opens
in the system browser (QDesktopServices.openUrl) with Stripe's success_url and
cancel_url pointed at this listener on a random 127.0.0.1 port. When the browser
lands on /return, we record success|cancel, show a friendly page, and signal the
app (via wait() or the on_return callback) to refresh entitlement.

The callback fires on the listener's worker thread; a Qt caller must marshal it
onto the GUI thread (e.g. emit a signal) rather than touching widgets directly.
Pure stdlib so it is testable headless, with no Qt dependency.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_RETURN_PAGE = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>AutoApply</title></head>"
    b"<body style='font-family:-apple-system,Segoe UI,sans-serif;"
    b"text-align:center;padding-top:80px;color:#1a1a1a'>"
    b"<h2>You're all set</h2>"
    b"<p>You can close this tab and return to AutoApply.</p>"
    b"</body></html>"
)


class _ReturnHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("/return", ""):
            self.send_response(404)
            self.end_headers()
            return
        status = (parse_qs(parsed.query).get("status") or ["success"])[0]
        self.server.record_return(status)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_RETURN_PAGE)

    def log_message(self, *args) -> None:  # silence default stderr access log
        return


class LoopbackReturnServer:
    """Bind 127.0.0.1:<random>, serve the return page once, expose the outcome.

    Lifecycle: start() -> hand success_url/cancel_url to Stripe -> wait()/callback
    -> stop(). Idempotent stop(); safe to call even if never started.
    """

    def __init__(self, on_return: Callable[[str], None] | None = None):
        self._on_return = on_return
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._event = threading.Event()
        self._status: str | None = None
        self._lock = threading.Lock()

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ReturnHandler)
        httpd.record_return = self._record  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="autoapply-checkout-return", daemon=True
        )
        self._thread.start()
        logger.info("checkout return listener on %s", self.base_url)
        return self.base_url

    def _record(self, status: str) -> None:
        with self._lock:
            first = self._status is None
            if first:
                self._status = status
        self._event.set()
        # Only the first (winning) return notifies; stray later hits are ignored.
        if first and self._on_return is not None:
            try:
                self._on_return(status)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the listener
                logger.exception("checkout return callback failed")

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("LoopbackReturnServer not started")
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def success_url(self) -> str:
        return f"{self.base_url}/return?status=success"

    @property
    def cancel_url(self) -> str:
        return f"{self.base_url}/return?status=cancel"

    def wait(self, timeout: float | None = None) -> str | None:
        """Block until a return is captured (or timeout). Returns the status or None."""
        if self._event.wait(timeout):
            return self._status
        return None

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
