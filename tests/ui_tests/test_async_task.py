"""The reusable off-UI-thread runner for one-shot blocking/async calls.

AsyncTaskRunner owns its own asyncio loop in a daemon thread (the proven
EngineWorker mechanism, but gate-free) so sign-in, profile/criteria save, file
copy, entitlement checks, and checkout launch never block the GUI thread. The
only hand-back is a Qt signal (succeeded/failed), delivered on the GUI thread.
"""

from __future__ import annotations

import pytest

from autoapply_next.ui.async_task import AsyncTaskRunner


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def test_submit_sync_fn_emits_succeeded_with_result_and_token(qtbot, runner):
    got: list = []
    runner.succeeded.connect(lambda res, tok: got.append((res, tok)))
    with qtbot.waitSignal(runner.succeeded, timeout=3000):
        runner.submit(lambda: 21 * 2, token="t1")
    assert got == [(42, "t1")]


def test_submit_sync_fn_failure_emits_failed_with_message(qtbot, runner):
    errs: list = []
    runner.failed.connect(lambda msg, tok: errs.append((msg, tok)))

    def boom():
        raise ValueError("nope")

    with qtbot.waitSignal(runner.failed, timeout=3000):
        runner.submit(boom, token="t2")
    assert errs and errs[0][1] == "t2"
    assert "nope" in errs[0][0]


async def _async_value():
    return "async-result"


def test_submit_coro_emits_succeeded(qtbot, runner):
    got: list = []
    runner.succeeded.connect(lambda res, tok: got.append(res))
    with qtbot.waitSignal(runner.succeeded, timeout=3000):
        runner.submit_coro(_async_value(), token="c1")
    assert got == ["async-result"]


def test_sync_fn_runs_off_the_gui_thread(qtbot, runner):
    import threading

    seen: list = []
    gui_thread = threading.current_thread().ident
    runner.succeeded.connect(lambda res, tok: seen.append(res))
    with qtbot.waitSignal(runner.succeeded, timeout=3000):
        runner.submit(lambda: threading.current_thread().ident, token="x")
    assert seen and seen[0] != gui_thread  # work ran on a worker thread


def test_stop_is_idempotent(qtbot):
    r = AsyncTaskRunner()
    r.stop()
    r.stop()  # must not raise
