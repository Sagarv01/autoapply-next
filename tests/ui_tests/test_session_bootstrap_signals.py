"""pytest-qt tests for EngineWorker.launch_session_browser.

These do not hit Seek. They monkey-patch `run_session_bootstrap` in
`worker.py` with a synthetic coroutine that emits status lines and returns
a chosen `SessionBootstrapResult`. The point is verifying the worker's
state transitions, the `log` and `session_finished` signal flow, and the
cancellation path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
)
from autoapply_next.engine.worker import EngineWorker


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _build_worker(workdir: Path) -> EngineWorker:
    return EngineWorker(engine_workdir=workdir)


def test_session_bootstrap_valid_path(qtbot, workdir, monkeypatch):
    async def fake(*, engine_workdir, on_status=None, is_cancelled=None):
        on_status and on_status("launching")
        await asyncio.sleep(0.01)
        on_status and on_status("logged in")
        return SessionBootstrapResult(
            status=SessionStatus.VALID,
            message="ok",
        )

    monkeypatch.setattr(worker_module, "run_session_bootstrap", fake)

    worker = _build_worker(workdir)
    try:
        logs: list[str] = []
        states: list[str] = []
        finished: list[SessionBootstrapResult] = []

        worker.log.connect(logs.append)
        worker.state_changed.connect(states.append)
        worker.session_finished.connect(finished.append)

        with qtbot.waitSignal(worker.session_finished, timeout=3000):
            worker.launch_session_browser()

        assert len(finished) == 1
        assert finished[0].status == SessionStatus.VALID
        assert "launching" in logs
        assert "logged in" in logs
        assert "running" in states
        qtbot.waitUntil(lambda: "idle" in states, timeout=2000)
    finally:
        worker.stop_loop()


def test_session_bootstrap_invalid_result(qtbot, workdir, monkeypatch):
    async def fake(*, engine_workdir, on_status=None, is_cancelled=None):
        return SessionBootstrapResult(
            status=SessionStatus.INVALID,
            message="login redirected",
        )

    monkeypatch.setattr(worker_module, "run_session_bootstrap", fake)

    worker = _build_worker(workdir)
    try:
        finished: list[SessionBootstrapResult] = []
        worker.session_finished.connect(finished.append)
        with qtbot.waitSignal(worker.session_finished, timeout=3000):
            worker.launch_session_browser()
        assert finished[0].status == SessionStatus.INVALID
    finally:
        worker.stop_loop()


def test_session_bootstrap_abandoned_result(qtbot, workdir, monkeypatch):
    async def fake(*, engine_workdir, on_status=None, is_cancelled=None):
        return SessionBootstrapResult(
            status=SessionStatus.ABANDONED,
            message="closed before login",
        )

    monkeypatch.setattr(worker_module, "run_session_bootstrap", fake)

    worker = _build_worker(workdir)
    try:
        finished: list[SessionBootstrapResult] = []
        worker.session_finished.connect(finished.append)
        with qtbot.waitSignal(worker.session_finished, timeout=3000):
            worker.launch_session_browser()
        assert finished[0].status == SessionStatus.ABANDONED
    finally:
        worker.stop_loop()


def test_session_bootstrap_cancel(qtbot, workdir, monkeypatch):
    async def fake(*, engine_workdir, on_status=None, is_cancelled=None):
        on_status and on_status("waiting for login")
        # Honor cancel.
        for _ in range(300):
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()
            await asyncio.sleep(0.05)
        return SessionBootstrapResult(
            status=SessionStatus.ABANDONED, message="(timeout)"
        )

    monkeypatch.setattr(worker_module, "run_session_bootstrap", fake)

    worker = _build_worker(workdir)
    try:
        finished: list[SessionBootstrapResult] = []
        worker.session_finished.connect(finished.append)

        with qtbot.waitSignal(worker.log, timeout=2000):
            worker.launch_session_browser()

        with qtbot.waitSignal(worker.session_finished, timeout=3000):
            worker.cancel()

        assert finished[0].status == SessionStatus.CANCELLED
    finally:
        worker.stop_loop()


def test_session_bootstrap_rejects_while_apply_running(qtbot, workdir, monkeypatch):
    """The worker is shared; only one operation at a time."""

    async def slow_apply(**kwargs):
        await asyncio.sleep(30)
        from autoapply_next.engine.results import (
            ApplicationResult,
            ApplicationStatus,
        )
        return ApplicationResult(
            job_url=kwargs["job_url"], status=ApplicationStatus.DRY_RUN_VERIFIED
        )

    async def fake_session(*, engine_workdir, on_status=None, is_cancelled=None):
        return SessionBootstrapResult(
            status=SessionStatus.VALID, message="ok"
        )

    monkeypatch.setattr(worker_module, "apply_to_job", slow_apply)
    monkeypatch.setattr(worker_module, "run_session_bootstrap", fake_session)

    worker = _build_worker(workdir)
    try:
        failed: list[tuple[str, str]] = []
        worker.failed.connect(lambda op, msg: failed.append((op, msg)))

        worker.run_job("https://au.seek.com/job/1", False)
        # Brief sleep so the apply task is in flight.
        qtbot.wait(100)
        with qtbot.waitSignal(worker.failed, timeout=2000):
            worker.launch_session_browser()

        assert any(op == "session" for op, _ in failed)
        worker.cancel()
    finally:
        worker.stop_loop()
