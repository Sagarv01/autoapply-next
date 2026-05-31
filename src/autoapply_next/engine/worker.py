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
from .batch import (
    BatchPreparedJob,
    BatchRunResult,
    prepare_batch,
    run_batch,
)
from .persistence import is_fatal_condition, queued_urls_for_batch
from .progress import ProgressEvent
from .results import ApplicationResult, ApplicationStatus
from .scraping import ScrapeResult, scrape_and_score
from .session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
    run_session_bootstrap,
)


# Mirrors vendor/job-finder/main.py:188 MAX_APPLIES_PER_RUN. Hard cap on a
# single scrape-and-auto-apply pass so a wide scrape cannot run for hours.
MAX_APPLIES_PER_RUN = 100

logger = logging.getLogger(__name__)


class EngineWorker(QObject):
    state_changed = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str, str)
    log = Signal(str)
    session_finished = Signal(object)
    scrape_finished = Signal(object)
    # Batch-flow signals. The (int, int, object) shape is (done, total, row).
    batch_prepare_progress = Signal(int, int, object)
    batch_prepare_finished = Signal(object)
    batch_apply_progress = Signal(int, int, object)
    batch_apply_finished = Signal(object)

    def __init__(self, *, engine_workdir: Path, match_threshold: int = 50):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._match_threshold = int(match_threshold)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._cancel_event = threading.Event()
        """Cooperative cancellation signal between stages. Set on `cancel()`,
        polled by `apply_to_job` via `is_cancelled=`."""
        self._stop_batch_event = threading.Event()
        """Graceful between-job stop signal for `run_batch`. The current
        job completes; the loop exits before the next one starts."""
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
        """Scrape Seek for one keyword, score results, persist to jobs.db.

        Does NOT auto-apply. Use `scrape_and_auto_apply` for the
        scrape-then-apply pipeline that mirrors job-finder's daemon.
        """
        if not self._can_start(label=f"scrape {keyword}"):
            self.failed.emit(
                "scrape", self._busy_message("scrape")
            )
            return
        self._begin("scrape")
        asyncio.run_coroutine_threadsafe(
            self._scrape_runner(keyword, location), self._loop
        )

    def scrape_and_auto_apply(
        self,
        keyword: str,
        *,
        location: str = "Australia",
        allow_real_submit: bool,
        throttle_seconds: int = 60,
        daily_cap: int = 0,
        max_jobs: int = MAX_APPLIES_PER_RUN,
    ) -> None:
        """Chained operation: scrape Seek for `keyword`, then auto-apply
        every queued job at or above the worker's `match_threshold` in
        score-descending order, mirroring `vendor/job-finder/main.py`.

        `allow_real_submit` flows straight through to the safety gate;
        the caller (typically QueueScreen) reads it from
        `SettingsStore.allow_real_submit`. The Settings checkbox + its
        confirmation dialog remain the master switch; nothing here
        bypasses it.

        Stop / cancel semantics inherit from the existing batch runner:
        `stop_batch()` halts after the current job, `cancel()` cancels
        the current job in flight. Circuit breaker (fatal classifier +
        consecutive failures) and pacing are unchanged.
        """
        if not self._can_start(label=f"scrape+apply {keyword}"):
            self.failed.emit(
                "scrape_and_auto_apply",
                self._busy_message("scrape-and-apply"),
            )
            return
        self._begin("scrape_and_auto_apply")
        self._stop_batch_event.clear()
        asyncio.run_coroutine_threadsafe(
            self._scrape_then_apply_runner(
                keyword=keyword,
                location=location,
                allow_real_submit=bool(allow_real_submit),
                throttle_seconds=int(throttle_seconds),
                daily_cap=int(daily_cap),
                max_jobs=int(max_jobs),
            ),
            self._loop,
        )

    def prepare_batch(self, min_score: int, max_jobs: int = 30) -> None:
        """Phase 1 of the batch flow: dry-run each queued job at or above
        `min_score`, build a BatchPreparedJob per row, surface via the
        batch_prepare_progress + batch_prepare_finished signals."""
        if not self._can_start(label=f"batch prepare>={min_score}"):
            self.failed.emit(
                "batch_prepare", self._busy_message("batch prepare")
            )
            return
        self._begin("batch_prepare")
        self._stop_batch_event.clear()
        asyncio.run_coroutine_threadsafe(
            self._batch_prepare_runner(min_score, max_jobs), self._loop
        )

    def run_batch(
        self,
        job_urls: list[str],
        allow_real_submit: bool,
        throttle_seconds: int = 20,
    ) -> None:
        """Phase 2 of the batch flow: submit the approved set one by one.

        `allow_real_submit` flows straight through to the safety gate; this
        method does not flip it implicitly. The caller is responsible for
        the user-facing confirmation dialog.
        """
        if not job_urls:
            self.failed.emit("batch_run", "No jobs approved.")
            return
        if not self._can_start(label=f"batch run x{len(job_urls)}"):
            self.failed.emit(
                "batch_run", self._busy_message("batch run")
            )
            return
        self._begin("batch_run")
        self._stop_batch_event.clear()
        asyncio.run_coroutine_threadsafe(
            self._batch_run_runner(
                list(job_urls), bool(allow_real_submit), int(throttle_seconds)
            ),
            self._loop,
        )

    def stop_batch(self) -> None:
        """Graceful stop for a running batch: the current job completes,
        no further jobs start. Distinct from cancel(), which cancels the
        current job in flight."""
        if self._get_state() != "running":
            return
        self._stop_batch_event.set()
        self.log.emit("STOP requested: batch will halt after the current job.")

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

    async def _batch_prepare_runner(self, min_score: int, max_jobs: int) -> None:
        self._current_task = asyncio.current_task()
        try:
            try:
                def on_progress(done, total, row):
                    self.batch_prepare_progress.emit(done, total, row)
                    self.log.emit(
                        f"[prepare {done}/{total}] {row.status}: {row.title}"
                    )

                rows = await prepare_batch(
                    engine_workdir=self._engine_workdir,
                    min_score=min_score,
                    on_progress=on_progress,
                    is_cancelled=self._cancel_event.is_set,
                    max_jobs=max_jobs,
                )
                # SafetyGate regression check: prepare runs with
                # allow_real_submit=False, so a SUBMITTED status leaking out is
                # a security regression. Workstream D's _result_to_prepared
                # records this as a failed row with the sentinel substring
                # "unexpected status submitted" in error_message (see Contract 5
                # coordination note in PARALLEL_PLAN.md). If we see any such row,
                # surface a loud failed signal so the UI pops a critical dialog,
                # in addition to emitting the prepared list.
                self._check_for_safety_gate_breach(rows)
                self.batch_prepare_finished.emit(rows)
            except asyncio.CancelledError:
                self.batch_prepare_finished.emit([])
            except Exception as exc:
                logger.exception("EngineWorker: batch_prepare crashed")
                self.failed.emit("batch_prepare", f"{type(exc).__name__}: {exc}")
        finally:
            self._current_task = None
            self._set_state("idle")

    def _check_for_safety_gate_breach(self, rows) -> None:
        """Scan prepared rows for the SafetyGate breach sentinel.

        Per Contract 5 coordination, batch._result_to_prepared marks any
        unexpected status (CANCELLED or SUBMITTED) from a dry-run prepare
        as status='failed' with error_message containing
        'unexpected status <value>'. A SUBMITTED leaking through dry-run
        means the safety gate failed; emit a CRITICAL log and a failed
        signal with op='SAFETY_GATE_BREACH' so the dialog is loud.
        """
        if not rows:
            return
        sentinel = "unexpected status submitted"
        for row in rows:
            err = getattr(row, "error_message", "") or ""
            note = getattr(row, "note", "") or ""
            status = getattr(row, "status", "") or ""
            haystack = f"{err} {note}".lower()
            if status == "failed" and sentinel in haystack:
                url = getattr(row, "url", "<unknown>")
                logger.critical(
                    "SAFETY_GATE_BREACH: SUBMITTED leaked through dry-run "
                    "prepare for url=%s; row.error_message=%s",
                    url,
                    err,
                )
                self.failed.emit(
                    "SAFETY_GATE_BREACH",
                    (
                        "Safety gate regression: a SUBMITTED status reached "
                        "the prepare phase, which is supposed to be dry-run "
                        f"only. URL: {url}. Detail: {err}"
                    ),
                )
                # One emission is enough; the dialog is meant to halt the user
                # and force them to investigate. Avoid spamming N dialogs.
                return

    async def _batch_run_runner(
        self,
        job_urls: list[str],
        allow_real_submit: bool,
        throttle_seconds: int,
    ) -> None:
        self._current_task = asyncio.current_task()
        # Construct the tally up front and hand it to run_batch so we keep a
        # reference to the live, mutated object. On every exit path (success,
        # cancel, exception) we emit THIS tally rather than constructing a
        # zeroed one, preserving the per-job entries that run_batch already
        # accumulated. See Contract 5 in docs/PARALLEL_PLAN.md.
        tally = BatchRunResult()
        # Resilience baseline mirrors job-finder APPLY_GAP_MIN/MAX. Clamp the
        # lower bound to at least 60s, then upper = lower + 60s. With the
        # default worker setting of 20, this becomes (60, 120) which is the
        # spirit of the engine pacing.
        lower = max(60, int(throttle_seconds))
        throttle_range = (lower, lower + 60)
        try:
            try:
                def on_progress(done, total, result):
                    self.batch_apply_progress.emit(done, total, result)
                    msg = f"[run {done}/{total}] {result.status.value}"
                    if result.error_message:
                        msg += f": {result.error_message}"
                    self.log.emit(msg)

                await run_batch(
                    job_urls=job_urls,
                    engine_workdir=self._engine_workdir,
                    allow_real_submit=allow_real_submit,
                    on_progress=on_progress,
                    is_cancelled=self._cancel_event.is_set,
                    is_stopped=self._stop_batch_event.is_set,
                    throttle_range_seconds=throttle_range,
                    tally=tally,
                )
                self.batch_apply_finished.emit(tally)
            except asyncio.CancelledError:
                # Emit the REAL tally that run_batch was filling in. It already
                # contains every per-job entry processed before the cancel
                # propagated, so the UI shows truth, not zeros.
                tally.stop_reason = "cancelled"
                self.batch_apply_finished.emit(tally)
            except Exception as exc:
                logger.exception("EngineWorker: batch_run crashed")
                self.failed.emit("batch_run", f"{type(exc).__name__}: {exc}")
        finally:
            self._current_task = None
            self._set_state("idle")

    async def _scrape_then_apply_runner(
        self,
        *,
        keyword: str,
        location: str,
        allow_real_submit: bool,
        throttle_seconds: int,
        daily_cap: int,
        max_jobs: int,
    ) -> None:
        """Worker-side chained runner. Mirrors job-finder/main.py:
        Phase 1 scrape; Phase 1.5 select score-desc; Phase 2 apply.

        On exit the worker emits batch_apply_finished with the TRUE tally
        (Contract 5), even on cancellation / fatal halt."""
        self._current_task = asyncio.current_task()
        tally = BatchRunResult()
        try:
            try:
                # Phase 1: scrape. Same path as scrape_and_score.
                scrape_result = await scrape_and_score(
                    keyword=keyword,
                    location=location,
                    engine_workdir=self._engine_workdir,
                    on_status=self._emit_log,
                    is_cancelled=self._cancel_event.is_set,
                )
                self.scrape_finished.emit(scrape_result)
                if self._cancel_event.is_set() or self._stop_batch_event.is_set():
                    tally.stop_reason = (
                        "cancelled" if self._cancel_event.is_set() else "user_stop"
                    )
                    self.batch_apply_finished.emit(tally)
                    return

                # Phase 1.5: enumerate eligible queued URLs in score-desc
                # order, capped at max_jobs (mirrors MAX_APPLIES_PER_RUN).
                urls = queued_urls_for_batch(
                    engine_workdir=self._engine_workdir,
                    min_score=self._match_threshold,
                )
                urls = urls[:max_jobs]
                if not urls:
                    self.log.emit(
                        f"Auto-apply: no queued jobs at score >= "
                        f"{self._match_threshold}; nothing to do."
                    )
                    self.batch_apply_finished.emit(tally)
                    return
                mode = "LIVE" if allow_real_submit else "dry-run"
                self.log.emit(
                    f"Auto-apply ({mode}): {len(urls)} job(s) at score "
                    f">= {self._match_threshold}, score-desc, "
                    f"throttle {throttle_seconds}-{throttle_seconds + 60}s, "
                    f"cap {max_jobs}, STOP available on Batch screen."
                )

                # Phase 2: apply. Same run_batch contract as the manual
                # batch flow; safety gate, circuit breaker, persistence,
                # tally all unchanged.
                def on_progress(done, total, result):
                    self.batch_apply_progress.emit(done, total, result)
                    msg = f"[run {done}/{total}] {result.status.value}"
                    if result.error_message:
                        msg += f": {result.error_message}"
                    self.log.emit(msg)

                lower = max(60, int(throttle_seconds))
                throttle_range = (lower, lower + 60)
                await run_batch(
                    job_urls=urls,
                    engine_workdir=self._engine_workdir,
                    allow_real_submit=allow_real_submit,
                    on_progress=on_progress,
                    is_cancelled=self._cancel_event.is_set,
                    is_stopped=self._stop_batch_event.is_set,
                    throttle_range_seconds=throttle_range,
                    tally=tally,
                    fatal_classifier=is_fatal_condition,
                    max_consecutive_failures=3,
                    daily_cap=daily_cap,
                )
                self.batch_apply_finished.emit(tally)
            except asyncio.CancelledError:
                tally.stop_reason = "cancelled"
                self.batch_apply_finished.emit(tally)
            except Exception as exc:
                logger.exception("EngineWorker: scrape+apply crashed")
                self.failed.emit(
                    "scrape_and_auto_apply",
                    f"{type(exc).__name__}: {exc}",
                )
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
