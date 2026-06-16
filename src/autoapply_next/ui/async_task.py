"""AsyncTaskRunner: run one-shot blocking/async work off the GUI thread.

A frozen wizard reads as a crash to a non-technical user, so NOTHING blocking may
run on the GUI thread. This helper owns its own asyncio event loop in a daemon
thread (the same mechanism EngineWorker uses, but WITHOUT the single-flight gate
that exists for the Chromium-singleton apply/scrape constraint) and hands every
result back via a Qt signal, which Qt delivers on the GUI thread through a queued
connection.

Use it for every blocking call in the wizard:
  - sync blocking calls (AuthManager.sign_in, profile/criteria/config save, resume
    /cover file copy): `submit(lambda: fn(args), token=...)` -> run via
    asyncio.to_thread so the loop stays responsive.
  - already-async calls (proxy_billing.fetch_subscription_status,
    checkout_flow.run_checkout): `submit_coro(coro, token=...)`.

CONTRACT for callers (enforced by review, not the type system):
  * Read all widget state (QLineEdit.text(), etc.) on the GUI thread BEFORE submit
    and pass it in; never read/touch widgets from inside the submitted callable.
  * Disable the triggering control on the GUI thread before submit; re-enable in
    the succeeded/failed slot. There is deliberately no built-in busy gate here.
The `token` lets one shared runner disambiguate which call returned.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class AsyncTaskRunner(QObject):
    succeeded = Signal(object, object)  # (result, token)
    failed = Signal(str, object)        # (error_message, token)

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run_loop, name="AsyncTaskRunner", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("AsyncTaskRunner event loop did not start")

    # -------------------------------------------------------------- loop
    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    # ----------------------------------------------------------- submit
    def submit(self, fn: Callable[[], Any], *, token: object = None) -> None:
        """Run a SYNC blocking callable off-thread (via asyncio.to_thread)."""
        self._schedule(asyncio.to_thread(fn), token)

    def submit_coro(self, coro: Awaitable[Any], *, token: object = None) -> None:
        """Schedule an already-async awaitable (e.g. run_checkout)."""
        self._schedule(coro, token)

    def _schedule(self, awaitable: Awaitable[Any], token: object) -> None:
        loop = self._loop
        if loop is None or self._stopped:
            self.failed.emit("Background runner is not available", token)
            return
        asyncio.run_coroutine_threadsafe(self._wrap(awaitable, token), loop)

    async def _wrap(self, awaitable: Awaitable[Any], token: object) -> None:
        try:
            result = await awaitable
        except Exception as exc:  # noqa: BLE001 - report every failure to the GUI
            logger.exception("AsyncTaskRunner task failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}", token)
            return
        self.succeeded.emit(result, token)

    # ------------------------------------------------------------- stop
    def stop(self) -> None:
        """Cancel any in-flight tasks, stop the loop, and join the thread.
        Idempotent and SHUTDOWN-ONLY: call once when the app is closing
        (MainWindow.closeEvent); do not submit afterwards."""
        if self._stopped:
            return
        self._stopped = True
        loop = self._loop
        if loop is not None and loop.is_running():
            # Drain in-flight tasks first so none is GC'd while pending (which
            # would emit a noisy "Task was destroyed but it is pending" warning).
            try:
                fut = asyncio.run_coroutine_threadsafe(self._drain(), loop)
                fut.result(timeout=2)
            except Exception:  # noqa: BLE001 - shutdown best-effort
                pass
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=5)

    async def _drain(self) -> None:
        me = asyncio.current_task()
        others = [t for t in asyncio.all_tasks() if t is not me]
        for t in others:
            t.cancel()
        for t in others:
            try:
                await t
            except BaseException:  # noqa: BLE001 - includes CancelledError
                pass
