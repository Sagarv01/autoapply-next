"""Qt worker that drives the engine on its own thread + asyncio loop.

See ADR-0003 for the threading model.

# Design (clean version)

The worker is a `QObject` that lives on whatever thread constructed it (the
GUI thread). It owns a *plain Python thread* (not a QThread) which spins an
asyncio event loop with `run_forever`. The GUI calls `run_job(url, allow)`
on the worker as a normal method; that schedules a coroutine on the asyncio
loop via `asyncio.run_coroutine_threadsafe`. Progress events come back as
Qt signal emissions from inside the coroutine; Qt auto-queues those to the
GUI thread because the receiving slots' QObject lives there.

Why not QThread + moveToThread + QMetaObject.invokeMethod? Because PySide6
slots only fire when the destination thread is running a Qt event loop; if
the worker thread is running the asyncio loop with `run_forever`, the Qt
event loop on that thread is parked, so queued slot invocations would
never arrive. The cleanest fix is to not put the worker on a QThread at
all; the asyncio loop is the worker's only event loop, and the GUI's slots
are called via signal emissions from the asyncio coroutines.

# Public Qt signals (auto-queued to whichever thread owns the connected slot)

- ``state_changed(str)``    one of: "idle", "running", "cancelling"
- ``progress(object)``      a `ProgressEvent`
- ``finished(object)``      an `ApplicationResult`
- ``failed(str, str)``      (job_url, error_message); for catastrophic failures
                             before a result could be built (e.g. EngineNotReady)
- ``log(str)``              optional textual log line for the live-progress tail

# Public methods (call from the GUI thread)

- ``run_job(job_url: str, allow_real_submit: bool) -> None``
- ``cancel() -> None``
- ``stop_loop() -> None``  must be called before the app quits

# Constraints

The worker is a singleton per process. The safety gate it installs is a
module-global monkey-patch on `seek_apply._submit`; two workers in the same
process would race. The GUI's MainWindow is responsible for enforcing
single-instance.
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

logger = logging.getLogger(__name__)


class EngineWorker(QObject):
    state_changed = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str, str)
    log = Signal(str)

    def __init__(self, *, engine_workdir: Path, match_threshold: int = 20):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._match_threshold = int(match_threshold)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._cancel_event = threading.Event()
        """Cooperative cancellation signal between stages. Set on `cancel()`,
        polled by `apply_to_job` via `is_cancelled=`."""
        self._current_task: asyncio.Task | None = None
        """The asyncio.Task currently running `_run_one`. Cancel() on this
        propagates `CancelledError` into the engine for in-flight cancellation."""
        self._state_lock = threading.Lock()
        self._state = "idle"

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

    def run_job(self, job_url: str, allow_real_submit: bool) -> None:
        """Schedule a job on the worker's loop. Re-entrancy is rejected with a
        `failed` signal."""
        if self._loop is None:
            self.failed.emit(job_url, "EngineWorker loop is not initialised")
            return
        if self._current_task is not None and not self._current_task.done():
            self.failed.emit(
                job_url,
                "EngineWorker busy: a job is already running. "
                "Cancel it before starting another.",
            )
            return
        self._cancel_event.clear()
        self._set_state("running")
        asyncio.run_coroutine_threadsafe(
            self._runner(job_url, allow_real_submit), self._loop
        )

    def cancel(self) -> None:
        """Request cancellation. Stops between stages (cooperative) and tears
        down the browser context in-flight (`asyncio.CancelledError` propagates
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
                # Drain any pending cancellations cleanly.
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

    async def _runner(self, job_url: str, allow_real_submit: bool) -> None:
        self._current_task = asyncio.current_task()
        try:
            await self._run_one(job_url, allow_real_submit)
        finally:
            self._current_task = None
            self._set_state("idle")

    async def _run_one(self, job_url: str, allow_real_submit: bool) -> None:
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
            # Do NOT re-raise: the `finally` in _runner needs to set state to idle
            # and Qt-thread observers expect a clean finished signal.
        except EngineNotReadyError as exc:
            self.failed.emit(job_url, f"Engine not ready: {exc}")
        except Exception as exc:
            logger.exception("EngineWorker: unhandled exception in _run_one")
            self.failed.emit(job_url, f"{type(exc).__name__}: {exc}")

    def _emit_progress(self, event: ProgressEvent) -> None:
        # Called from inside the asyncio coroutine on the worker thread.
        # Qt auto-queues the signal to slots on other threads.
        self.progress.emit(event)
        if event.message:
            self.log.emit(f"[{event.stage.value}] {event.message}")

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            if state == self._state:
                return
            self._state = state
        self.state_changed.emit(state)

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state
