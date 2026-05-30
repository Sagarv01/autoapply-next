"""Qt worker that drives the engine on its own thread + asyncio loop.

See ADR-0003 for the threading model.

# Design (clean version)

The worker is a `QObject` that lives on whatever thread constructed it (the
GUI thread). It owns a *plain Python thread* (not a QThread) which spins an
asyncio event loop with `run_forever`. The GUI calls `run_job(url, allow)`,
`launch_session_browser()`, or `scrape_and_score(keyword)` on the worker as
normal methods; each schedules a coroutine on the asyncio loop via
`asyncio.run_coroutine_threadsafe`. Progress comes back as Qt signal
emissions from inside the coroutines; Qt auto-queues those to the GUI
thread because the receiving slots' QObject lives there.

# Public Qt signals

- ``state_changed(str)``        one of: "idle", "running", "cancelling"
- ``progress(object)``          a `ProgressEvent` (apply jobs only)
- ``finished(object)``          an `ApplicationResult` (apply jobs only)
- ``failed(str, str)``          (operation_label, error_message)
- ``log(str)``                  textual status line; used by all three operations
- ``session_finished(object)``  a `SessionBootstrapResult`
- ``scrape_finished(object)``   a `ScrapeResult`

# Public methods (call from the GUI thread)

- ``run_job(job_url: str, allow_real_submit: bool) -> None``
- ``launch_session_browser() -> None``
- ``scrape_and_score(keyword: str, location: str = "Australia") -> None``
- ``cancel() -> None``
- ``stop_loop() -> None``  must be called before the app quits

# Constraints

The worker is a singleton per process. The safety gate it installs is a
module-global monkey-patch on `seek_apply._submit`; two workers in the same
process would race. The Chromium user-data-dir is also shared, so two
worker operations cannot run concurrently. The worker enforces this via
its `_current_task` check; concurrent requests are rejected with `failed`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .adapter import EngineNotReadyError, apply_to_job
from .progress import ProgressEvent
from .results import ApplicationResult, ApplicationStatus
from .scraping import ScrapeResult, scrape_and_score
from .session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
    run_session_bootstrap,
)

logger = logging.getLogger(__name__)


class EngineWorker(QObject):
    state_changed = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str, str)
    log = Signal(str)
    session_finished = Signal(object)
    scrape_finished = Signal(object)

    def __init__(self, *, engine_workdir: Path, match_threshold: int = 50):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._match_threshold = int(match_threshold)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._cancel_event = threading.Event()
        """Cooperative cancellation signal between stages. Set on `cancel()`,
        polled by `apply_to_job` via `is_cancelled=`."""
        self._current_task: asyncio.Task | None = None
        """The asyncio.Task currently running an operation. Cancel() on this
        propagates `CancelledError` into the engine for in-flight cancellation."""
        self._state_lock = threading.Lock()
        self._state = "idle"
        self._current_label = ""
        """Short label for the current operation, used in `failed.emit`."""

        self._asyncio_thread = threading.Thread(
            target=self._loop_main,
            daemon=True,
            name="EngineWorker-asyncio",
        )
        self._asyncio_thread.start()
        # Block briefly until the loop is ready so the caller can safely call
        # run_job immediately after construction.
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError("EngineWorker asyncio loop did not start in time")

    # -------------------------------------------------------- public methods

    def set_match_threshold(self, threshold: int) -> None:
        self._match_threshold = max(0, min(100, int(threshold)))

    def run_job(self, job_url: str, allow_real_submit: bool) -> None:
        """Schedule an apply job on the worker's loop. Re-entrancy is rejected
        with a `failed` signal."""
        if not self._can_start(label=f"apply {job_url}"):
            self.failed.emit(
                job_url,
                self._busy_message("apply"),
            )
            return
        self._begin("apply")
        asyncio.run_coroutine_threadsafe(
            self._apply_runner(job_url, allow_real_submit), self._loop
        )

    def launch_session_browser(self) -> None:
        """Open Chromium for manual Seek login, then verify."""
        if not self._can_start(label="session"):
            self.failed.emit(
                "session", self._busy_message("session bootstrap")
            )
            return
        self._begin("session")
        asyncio.run_coroutine_threadsafe(
            self._session_runner(), self._loop
        )

    def scrape_and_score(
        self, keyword: str, location: str = "Australia"
    ) -> None:
        """Scrape Seek for one keyword, score results, persist to jobs.db."""
        if not self._can_start(label=f"scrape {keyword}"):
            self.failed.emit(
                "scrape", self._busy_message("scrape")
            )
            return
        self._begin("scrape")
        asyncio.run_coroutine_threadsafe(
            self._scrape_runner(keyword, location), self._loop
        )

    def cancel(self) -> None:
        """Request cancellation. Stops between stages (cooperative) and tears
        down browser contexts in-flight (`asyncio.CancelledError` propagates
        through the engine's `finally` blocks)."""
        if self._get_state() != "running":
            return
        self._set_state("cancelling")
        self._cancel_event.set()
        loop = self._loop
        task = self._current_task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    def stop_loop(self) -> None:
        """Stop the asyncio loop and join its thread. Idempotent."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        self._asyncio_thread.join(timeout=5)

    # ------------------------------------------------------- internals

    def _loop_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    def _can_start(self, *, label: str) -> bool:
        if self._loop is None:
            return False
        if self._current_task is not None and not self._current_task.done():
            return False
        return True

    def _begin(self, label: str) -> None:
        self._current_label = label
        self._cancel_event.clear()
        self._set_state("running")

    def _busy_message(self, operation: str) -> str:
        return (
            f"EngineWorker busy with '{self._current_label}'. Cancel it before "
            f"starting a new {operation}."
        )

    async def _apply_runner(self, job_url: str, allow_real_submit: bool) -> None:
        self._current_task = asyncio.current_task()
        try:
            try:
                result = await apply_to_job(
                    job_url=job_url,
                    engine_workdir=self._engine_workdir,
                    on_progress=self._emit_progress,
                    is_cancelled=self._cancel_event.is_set,
                    allow_real_submit=allow_real_submit,
                    match_threshold=self._match_threshold,
                )
                self.finished.emit(result)
            except asyncio.CancelledError:
                cancelled = ApplicationResult(
                    job_url=job_url,
                    status=ApplicationStatus.CANCELLED,
                    error_message="Cancelled by user",
                )
                self.finished.emit(cancelled)
            except EngineNotReadyError as exc:
                self.failed.emit(job_url, f"Engine not ready: {exc}")
            except Exception as exc:
                logger.exception("EngineWorker: unhandled exception in apply")
                self.failed.emit(job_url, f"{type(exc).__name__}: {exc}")
        finally:
            self._current_task = None
            self._set_state("idle")

    async def _session_runner(self) -> None:
        self._current_task = asyncio.current_task()
        try:
            try:
                result = await run_session_bootstrap(
                    engine_workdir=self._engine_workdir,
                    on_status=self._emit_log,
                    is_cancelled=self._cancel_event.is_set,
                )
                self.session_finished.emit(result)
            except asyncio.CancelledError:
                self.session_finished.emit(
                    SessionBootstrapResult(
                        status=SessionStatus.CANCELLED,
                        message="Session bootstrap cancelled.",
                    )
                )
            except Exception as exc:
                logger.exception("EngineWorker: unhandled exception in session")
                self.failed.emit("session", f"{type(exc).__name__}: {exc}")
        finally:
            self._current_task = None
            self._set_state("idle")

    async def _scrape_runner(self, keyword: str, location: str) -> None:
        self._current_task = asyncio.current_task()
        try:
            try:
                result = await scrape_and_score(
                    keyword=keyword,
                    location=location,
                    engine_workdir=self._engine_workdir,
                    on_status=self._emit_log,
                    is_cancelled=self._cancel_event.is_set,
                )
                self.scrape_finished.emit(result)
            except asyncio.CancelledError:
                # Emit an empty result so the UI can stop spinning.
                self.scrape_finished.emit(
                    ScrapeResult(
                        keyword=keyword,
                        total_scraped=0,
                        new_jobs=0,
                        scored=[],
                        errors=["cancelled"],
                    )
                )
            except Exception as exc:
                logger.exception("EngineWorker: unhandled exception in scrape")
                self.failed.emit("scrape", f"{type(exc).__name__}: {exc}")
        finally:
            self._current_task = None
            self._set_state("idle")

    def _emit_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)
        if event.message:
            self.log.emit(f"[{event.stage.value}] {event.message}")

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            if state == self._state:
                return
            self._state = state
        self.state_changed.emit(state)

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state
