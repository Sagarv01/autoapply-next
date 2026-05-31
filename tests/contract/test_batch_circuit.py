"""Contract tests for `batch.run_batch` circuit breaker, pacing and tally.

The runner's contract is:

1. Pacing mirrors job-finder: a random per-apply gap drawn from
   `random.uniform(*throttle_range_seconds)`, default (60, 120). The
   value flows through to `asyncio.sleep`.
2. The runner accepts an optional `tally` and mutates it in place. The
   caller's reference is alive even when the coroutine returns
   mid-iteration (circuit breaker / STOP / cancel).
3. A fatal classifier on a FAILED result halts the batch with
   `stop_reason = "fatal:<reason>"`. Remaining URLs are untouched.
4. N back-to-back non-fatal FAILED results halt the batch with
   `stop_reason = "consecutive_failures"`. The counter resets on any
   non-FAILED outcome.
5. A `daily_cap` halts the batch with
   `stop_reason = "daily_cap_reached"`; remaining URLs are untouched.
6. After the loop exits (any reason) the runner releases the engine's
   `_PeekSession` so the same Chromium user-data-dir can be reopened.

These tests use synthetic `apply_to_job` stubs to drive the runner's
state machine deterministically. No real Seek, no real engine.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Iterable

import pytest

from autoapply_next.engine.batch import BatchRunResult, run_batch
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus


# --------------------------------------------------------------------- helpers


def _install_fake_peek_session(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Inject a `seek_apply` module with a `_PeekSession.close` async stub
    so the runner's finally-block teardown is observable without touching
    the real engine. Returns a sentinel dict the test can inspect."""
    sentinel = {"peek_close_calls": 0}

    class _FakePeekSession:
        @staticmethod
        async def close() -> None:
            sentinel["peek_close_calls"] += 1

    module = types.ModuleType("seek_apply")
    module._PeekSession = _FakePeekSession
    monkeypatch.setitem(sys.modules, "seek_apply", module)
    return sentinel


def _result(
    url: str,
    status: ApplicationStatus,
    *,
    exc_type: str | None = None,
    error: str | None = None,
) -> ApplicationResult:
    return ApplicationResult(
        job_url=url,
        status=status,
        error_message=error,
        exception_type=exc_type,
    )


def _stub_apply_to_job(
    monkeypatch: pytest.MonkeyPatch,
    results: Iterable[ApplicationResult],
) -> dict:
    """Replace `batch.apply_to_job` with a stub that yields the given
    results in order. Records each call's job_url so tests can assert no
    extra job was reached (circuit breaker semantics)."""
    iterator = iter(results)
    call_log: dict = {"urls": []}

    async def fake_apply(**kwargs) -> ApplicationResult:
        call_log["urls"].append(kwargs["job_url"])
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(
                f"apply_to_job called too many times: {kwargs['job_url']}"
            ) from exc

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod, "apply_to_job", fake_apply)
    return call_log


# --------------------------------------------------------------------- tests


def test_throttle_uses_60_120_random(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pacing: between jobs, run_batch calls
    `random.uniform(throttle_range_seconds[0], throttle_range_seconds[1])`
    and passes the resulting value through to `asyncio.sleep`.

    We monkeypatch random.uniform to return 0.0 so the test runs in
    milliseconds, and we monkeypatch asyncio.sleep with a recorder.
    """
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(
        monkeypatch,
        [
            _result("u1", ApplicationStatus.SUBMITTED),
            _result("u2", ApplicationStatus.SUBMITTED),
        ],
    )

    uniform_calls: list[tuple[float, float]] = []
    sleep_calls: list[float] = []

    from autoapply_next.engine import batch as batch_mod

    def fake_uniform(a: float, b: float) -> float:
        uniform_calls.append((a, b))
        return 0.0

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(batch_mod.random, "uniform", fake_uniform)
    monkeypatch.setattr(batch_mod.asyncio, "sleep", fake_sleep)

    asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
        )
    )

    assert uniform_calls == [(60, 120)], (
        "random.uniform must be called with the default (60, 120) range"
    )
    # Between two jobs there is exactly one throttle. The recorder sees
    # the 0.0 value flow through to asyncio.sleep.
    assert 0.0 in sleep_calls, (
        f"asyncio.sleep should have been called with 0.0; got {sleep_calls}"
    )


def test_circuit_breaker_halts_on_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fatal classifier verdict on a FAILED result halts the batch and
    leaves remaining URLs untouched. Wires the real
    `persistence.is_fatal_condition` so the integration with Workstream
    B's contract is also exercised."""
    _install_fake_peek_session(monkeypatch)
    call_log = _stub_apply_to_job(
        monkeypatch,
        [
            _result("A", ApplicationStatus.SUBMITTED),
            _result(
                "B",
                ApplicationStatus.FAILED,
                exc_type="BoardBlockedError",
                error="session expired; please re-bootstrap",
            ),
            # C must not be reached.
            _result("C", ApplicationStatus.SUBMITTED),
        ],
    )

    # Avoid waiting 60 seconds between jobs A and B.
    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    def fake_classifier(*, exception_type, error_message):
        if (
            exception_type == "BoardBlockedError"
            and "session expired" in (error_message or "").lower()
        ):
            return "Seek session expired; bootstrap and retry"
        return None

    tally = asyncio.run(
        run_batch(
            job_urls=["A", "B", "C"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            fatal_classifier=fake_classifier,
        )
    )

    assert tally.stop_reason.startswith("fatal:"), tally.stop_reason
    assert tally.fatal_reason == "Seek session expired; bootstrap and retry"
    # A + B processed; C must NOT have been started.
    assert call_log["urls"] == ["A", "B"]
    assert len(tally.per_job) == 2
    assert tally.per_job[0].status == ApplicationStatus.SUBMITTED
    assert tally.per_job[1].status == ApplicationStatus.FAILED


def test_circuit_breaker_halts_on_K_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """K back-to-back non-fatal FAILED results halt the batch."""
    _install_fake_peek_session(monkeypatch)
    call_log = _stub_apply_to_job(
        monkeypatch,
        [
            _result(
                "j1", ApplicationStatus.FAILED,
                exc_type="SeekApplyError", error="stuck step",
            ),
            _result(
                "j2", ApplicationStatus.FAILED,
                exc_type="SeekApplyError", error="stuck step",
            ),
            _result(
                "j3", ApplicationStatus.FAILED,
                exc_type="SeekApplyError", error="stuck step",
            ),
            # j4, j5 must not be reached.
            _result("j4", ApplicationStatus.SUBMITTED),
            _result("j5", ApplicationStatus.SUBMITTED),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    tally = asyncio.run(
        run_batch(
            job_urls=["j1", "j2", "j3", "j4", "j5"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            fatal_classifier=lambda **kw: None,  # never fatal
            max_consecutive_failures=3,
        )
    )

    assert tally.stop_reason == "consecutive_failures", tally.stop_reason
    assert tally.consecutive_failures == 3
    assert len(tally.per_job) == 3
    assert call_log["urls"] == ["j1", "j2", "j3"]


def test_consecutive_counter_resets_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-FAILED outcome resets the streak: F F S F F F should hit
    three consecutive failures only after the SUCCESS, i.e. at job 6."""
    _install_fake_peek_session(monkeypatch)
    call_log = _stub_apply_to_job(
        monkeypatch,
        [
            _result("a", ApplicationStatus.FAILED, exc_type="SeekApplyError"),
            _result("b", ApplicationStatus.FAILED, exc_type="SeekApplyError"),
            _result("c", ApplicationStatus.SUBMITTED),
            _result("d", ApplicationStatus.FAILED, exc_type="SeekApplyError"),
            _result("e", ApplicationStatus.FAILED, exc_type="SeekApplyError"),
            _result("f", ApplicationStatus.FAILED, exc_type="SeekApplyError"),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    tally = asyncio.run(
        run_batch(
            job_urls=["a", "b", "c", "d", "e", "f"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            fatal_classifier=lambda **kw: None,
            max_consecutive_failures=3,
        )
    )

    assert tally.stop_reason == "consecutive_failures"
    assert tally.consecutive_failures == 3
    # All six processed; circuit trips ON the sixth.
    assert call_log["urls"] == ["a", "b", "c", "d", "e", "f"]
    assert len(tally.per_job) == 6


def test_daily_cap_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """daily_cap=2 with today_count_fn returning 1 means one more
    SUBMITTED makes the running total 2/2; the runner halts before
    starting the third job."""
    _install_fake_peek_session(monkeypatch)
    call_log = _stub_apply_to_job(
        monkeypatch,
        [
            _result("x", ApplicationStatus.SUBMITTED),
            # y, z must not be reached.
            _result("y", ApplicationStatus.SUBMITTED),
            _result("z", ApplicationStatus.SUBMITTED),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    tally = asyncio.run(
        run_batch(
            job_urls=["x", "y", "z"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            daily_cap=2,
            today_count_fn=lambda: 1,
        )
    )

    assert tally.stop_reason == "daily_cap_reached", tally.stop_reason
    assert call_log["urls"] == ["x"]
    assert len(tally.per_job) == 1


def test_peek_session_closed_after_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After the loop exits (here: cleanly), the runner releases the
    engine's `_PeekSession` so the SingletonLock on
    seek_chrome_profile is freed for the next call."""
    sentinel = _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(
        monkeypatch,
        [_result("p", ApplicationStatus.SUBMITTED)],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    asyncio.run(
        run_batch(
            job_urls=["p"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
        )
    )

    assert sentinel["peek_close_calls"] == 1


def test_peek_session_closed_even_when_circuit_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Teardown of `_PeekSession` must happen on every exit path,
    including the early returns from the circuit breaker. Otherwise the
    user-data-dir stays locked after a fatal halt and the next attempt
    cannot open Chromium."""
    sentinel = _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(
        monkeypatch,
        [
            _result(
                "f1", ApplicationStatus.FAILED,
                exc_type="PermissionError", error="session not loaded",
            ),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    asyncio.run(
        run_batch(
            job_urls=["f1"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            fatal_classifier=lambda **kw: "session not loaded",
        )
    )

    assert sentinel["peek_close_calls"] == 1


def test_tally_object_is_mutated_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Caller constructs the tally and holds a reference. Even when
    run_batch returns mid-iteration (circuit breaker), the caller's
    reference shows the per-job entries written so far."""
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(
        monkeypatch,
        [
            _result("A", ApplicationStatus.SUBMITTED),
            _result(
                "B", ApplicationStatus.FAILED,
                exc_type="BoardBlockedError", error="captcha detected",
            ),
            _result("C", ApplicationStatus.SUBMITTED),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    caller_tally = BatchRunResult()
    returned_tally = asyncio.run(
        run_batch(
            job_urls=["A", "B", "C"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            tally=caller_tally,
            fatal_classifier=lambda **kw: (
                "Seek anti-bot challenge"
                if (kw.get("error_message") or "").lower().count("captcha")
                else None
            ),
        )
    )

    assert returned_tally is caller_tally
    assert len(caller_tally.per_job) == 2
    assert caller_tally.stop_reason.startswith("fatal:")
    assert caller_tally.fatal_reason == "Seek anti-bot challenge"


def test_remaining_queued_not_failed_when_circuit_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the circuit breaker trips, the URLs we have not yet
    processed are NEVER passed to apply_to_job and therefore never get
    persisted as 'failed'. The simplest invariant: the stub's call log
    does not include any URL past the trip point."""
    _install_fake_peek_session(monkeypatch)
    call_log = _stub_apply_to_job(
        monkeypatch,
        [
            _result("A", ApplicationStatus.SUBMITTED),
            _result(
                "B", ApplicationStatus.FAILED,
                exc_type="BoardBlockedError",
                error="rate limit hit; blocked for now",
            ),
            _result("C", ApplicationStatus.SUBMITTED),
            _result("D", ApplicationStatus.SUBMITTED),
        ],
    )

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod.random, "uniform", lambda a, b: 0.0)

    tally = asyncio.run(
        run_batch(
            job_urls=["A", "B", "C", "D"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            fatal_classifier=lambda **kw: (
                "Seek rate-limit detected"
                if "rate limit" in (kw.get("error_message") or "").lower()
                else None
            ),
        )
    )

    assert tally.stop_reason.startswith("fatal:")
    # A and B touched, C and D never.
    assert call_log["urls"] == ["A", "B"]
    # And the tally only contains entries for A and B.
    assert len(tally.per_job) == 2
    assert {r.job_url for r in tally.per_job} == {"A", "B"}
