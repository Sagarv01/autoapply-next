"""M-C.3: the HELD application status (a held screening question aborts apply).

A held question is NOT a failure: apply_to_job returns ApplicationStatus.HELD,
persistence maps it to the jobs.db 'held' status (excluded from the queued batch,
resumable once answered), and run_batch tallies it as held without tripping the
circuit breaker or counting a submission.
"""

from __future__ import annotations

import asyncio

from autoapply_next.engine import batch as batch_mod
from autoapply_next.engine.batch import run_batch
from autoapply_next.engine.persistence import TERMINAL_STATUSES, map_status
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from tests.contract.test_throttle_floor import (
    _install_fake_peek_session,
    _stub_apply_to_job,
)


def _held(url: str) -> ApplicationResult:
    return ApplicationResult(job_url=url, status=ApplicationStatus.HELD)


def test_map_status_held():
    assert map_status(_held("u1")) == "held"


def test_held_is_not_terminal():
    # held is resumable (answer-unblocks-resume), so it must not be terminal.
    assert "held" not in TERMINAL_STATUSES


def _noop_throttle(monkeypatch):
    async def fake(seconds, is_stopped, is_cancelled):
        return None

    monkeypatch.setattr(batch_mod, "_async_throttle", fake)


def test_run_batch_tallies_held_without_failing_or_submitting(monkeypatch, tmp_path):
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_held("u1"), _held("u2"), _held("u3")])
    _noop_throttle(monkeypatch)

    tally = asyncio.run(
        run_batch(
            job_urls=["u1", "u2", "u3"],
            engine_workdir=tmp_path,
            allow_real_submit=True,
            throttle_range_seconds=(0, 0),
            max_consecutive_failures=3,
        )
    )
    assert tally.held == 3
    assert tally.failed == 0
    assert tally.submitted == 0
    assert tally.stop_reason == "completed"  # held never trips the circuit breaker


def test_held_does_not_count_against_daily_cap(monkeypatch, tmp_path):
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_held("u1"), _held("u2")])
    _noop_throttle(monkeypatch)

    tally = asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=True,
            throttle_range_seconds=(0, 0),
            daily_cap=1,  # would trip after 1 SUBMISSION; held must not count
            today_count_fn=lambda: 0,
        )
    )
    assert tally.held == 2
    assert tally.stop_reason == "completed"
