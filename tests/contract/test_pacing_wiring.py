"""M-D: the time-of-day pacing gate is wired into the run_batch chokepoint.

Live submits take their inter-submit gap from pacing.next_submit_gap (time-of-day
aware, never below the 60s floor); dry-run keeps the configurable band and never
consults pacing. The existing layer-4 floor proofs (test_throttle_floor) stay
green because pacing's bands start at the floor and use the same random.uniform.
"""

from __future__ import annotations

import asyncio

from autoapply_next.engine import batch as batch_mod
from autoapply_next.engine.batch import run_batch
from tests.contract.test_throttle_floor import (
    _dry_run,
    _install_fake_peek_session,
    _stub_apply_to_job,
    _submitted,
)


def _noop_throttle(monkeypatch):
    async def fake(seconds, is_stopped, is_cancelled):
        return None

    monkeypatch.setattr(batch_mod, "_async_throttle", fake)


def test_live_run_takes_gap_from_time_of_day_pacing(monkeypatch, tmp_path):
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_submitted("u1"), _submitted("u2")])
    _noop_throttle(monkeypatch)

    seen = []

    def fake_pacing(now, rng=None):
        seen.append(now)
        return 73.0

    monkeypatch.setattr(batch_mod.pacing, "next_submit_gap", fake_pacing)

    asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=True,
            throttle_range_seconds=(0, 0),
        )
    )
    # one gap between the two submits, sourced from the pacing gate
    assert len(seen) == 1


def test_dry_run_does_not_consult_pacing(monkeypatch, tmp_path):
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_dry_run("u1"), _dry_run("u2")])
    _noop_throttle(monkeypatch)

    called = []
    monkeypatch.setattr(
        batch_mod.pacing, "next_submit_gap", lambda *a, **k: called.append(1) or 60.0
    )

    asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            throttle_range_seconds=(0, 0),
        )
    )
    assert called == []  # dry-run uses the configurable band, not the live gate
