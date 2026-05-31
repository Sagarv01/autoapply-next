"""pytest-qt tests for the batch flow: prepare, approve, run, STOP.

No live Seek. We stub `prepare_batch` and `run_batch` in `engine.worker` so
the worker emits its signals with synthetic data. The BatchScreen is
exercised end to end against those.

What we assert (per the brief):
- Prepare populates the table; the cover-letter / Q&A preview shows for a
  selected row.
- Approve toggling: Select all / Deselect all + per-row checkbox updates
  the "Submit N" button label and enabled state.
- Run submits only approved jobs (the synthetic run_batch records the
  input list; we assert it matches the user's selection).
- The real `safety.SafetyGate._submit` is NEVER called during the test.
- STOP requested mid-batch reaches `stop_batch_event` and the run loop
  stops between jobs.
- Failed / skipped rows are tallied and never re-submitted.
- Default match_threshold reads back as 10.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.batch import BatchPreparedJob, BatchRunResult
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from autoapply_next.engine.worker import EngineWorker
from autoapply_next.ui.batch_screen import BatchScreen
from autoapply_next.ui.settings_store import SettingsStore


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    # Minimal workdir layout. Even if the batch screen does not touch
    # config.yaml directly, the worker's adapter would.
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "profile.txt").write_text("test", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "candidate:\n  name: Test\n  email: t@t.com\n  phone: '+61'\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def settings(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "ui-settings.json")


@pytest.fixture
def stub_batch(monkeypatch):
    """Replace prepare_batch + run_batch in `engine.worker`. The synthetic
    prepare returns three rows: ready, not_quick_apply, ready. Run records
    whatever URLs it was called with and lets the test inspect."""

    recorded = {"prepare_calls": 0, "run_calls": [], "submit_invoked": False}

    async def fake_prepare(*, engine_workdir, min_score, on_progress,
                            is_cancelled, max_jobs=30):
        recorded["prepare_calls"] += 1
        rows = [
            BatchPreparedJob(
                url="https://au.seek.com/job/A",
                title="Job A", company="Acme",
                score=80, reasoning="great", status="ready",
                cover_letter_text="Dear Hiring Team,\n\nA cover.",
                screening_answers=[
                    {"question": "Are you eligible?", "answer": "Yes",
                     "source": "hard-rule"}
                ],
            ),
            BatchPreparedJob(
                url="https://au.seek.com/job/B",
                title="External-ATS Job", company="Beta",
                score=70, reasoning="ok", status="not_quick_apply",
                error_message="Job is not a Seek quick-apply listing",
                exception_type="JobNotQuickApplyError",
            ),
            BatchPreparedJob(
                url="https://au.seek.com/job/C",
                title="Job C", company="Gamma",
                score=50, reasoning="good", status="ready",
                cover_letter_text="Dear Hiring Team,\n\nC cover.",
            ),
        ]
        for i, row in enumerate(rows, 1):
            on_progress(i, len(rows), row)
            await asyncio.sleep(0)
        return rows

    async def fake_run(*, job_urls, engine_workdir, allow_real_submit,
                       on_progress, is_cancelled, is_stopped,
                       throttle_range_seconds=(0, 0), tally=None, **_kw):
        # Updated for D's Contract 5: run_batch now takes throttle_range_seconds
        # (tuple) instead of throttle_seconds (int) AND accepts a caller-owned
        # tally that is mutated in place (E's worker uses this). The stub also
        # accepts **_kw so new optional kwargs (fatal_classifier, daily_cap,
        # etc.) don't break the recording.
        recorded["run_calls"].append({
            "urls": list(job_urls),
            "allow_real_submit": bool(allow_real_submit),
            "throttle_range_seconds": tuple(throttle_range_seconds),
        })
        if tally is None:
            tally = BatchRunResult()
        for i, url in enumerate(job_urls, 1):
            if is_stopped():
                tally.stop_reason = "user_stop"
                return tally
            # Synthetic result: dry_run_verified for every job because the
            # test runs with allow_real_submit=False unless explicitly set.
            if allow_real_submit:
                # Test must never set this true; but if it does, we still
                # do NOT call the real engine -- mark as SUBMITTED so the
                # test failure surface is clear.
                recorded["submit_invoked"] = True
                result = ApplicationResult(
                    job_url=url, status=ApplicationStatus.SUBMITTED,
                )
                tally.submitted += 1
                tally.verified += 1
            else:
                result = ApplicationResult(
                    job_url=url, status=ApplicationStatus.DRY_RUN_VERIFIED,
                )
                tally.dry_run_verified += 1
            tally.per_job.append(result)
            on_progress(i, len(job_urls), result)
            await asyncio.sleep(0.005)
        return tally

    monkeypatch.setattr(worker_module, "prepare_batch", fake_prepare)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)
    return recorded


@pytest.fixture
def worker(workdir):
    w = EngineWorker(engine_workdir=workdir)
    yield w
    w.stop_loop()


# ---------------------------------------------------------------------- tests


def test_match_threshold_default_is_ten(tmp_path):
    s = SettingsStore(tmp_path / "ui-settings.json")
    assert s.match_threshold == 10, (
        "Default match_threshold must be 10; the batch prepare relies on it"
    )


def test_prepare_populates_table_and_enables_select_all(
    qtbot, workdir, worker, settings, stub_batch
):
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    # 3 rows from the stub.
    assert screen._table.rowCount() == 3
    # Two ready, one not_quick_apply.
    assert sum(1 for r in screen._prepared if r.ready) == 2
    # Select-all button now enabled because there are ready rows.
    assert screen._select_all_btn.isEnabled()


def test_select_all_only_ticks_ready_rows(
    qtbot, workdir, worker, settings, stub_batch
):
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    screen._on_select_all_clicked()
    qtbot.wait(20)
    checked = [cb.isChecked() for cb in screen._row_checkboxes]
    statuses = [r.status for r in screen._prepared]
    # ready (True), not_quick_apply (False), ready (True)
    assert checked == [True, False, True]
    assert statuses == ["ready", "not_quick_apply", "ready"]


def test_deselect_all_unticks(qtbot, workdir, worker, settings, stub_batch):
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    screen._on_select_all_clicked()
    qtbot.wait(20)
    screen._on_deselect_all_clicked()
    qtbot.wait(20)
    assert not any(cb.isChecked() for cb in screen._row_checkboxes)


def test_submit_button_label_tracks_count_and_gate(
    qtbot, workdir, worker, settings, stub_batch
):
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    # 0 selected.
    assert "Submit 0" in screen._submit_btn.text()
    assert not screen._submit_btn.isEnabled()
    # Select all.
    screen._on_select_all_clicked()
    qtbot.wait(20)
    assert "Submit 2" in screen._submit_btn.text()
    assert "dry-run" in screen._submit_btn.text().lower()
    assert screen._submit_btn.isEnabled()
    # Flip the gate WITHOUT going through the modal confirmation: directly
    # set the setting. The label must update.
    settings.allow_real_submit = True
    qtbot.wait(20)
    assert "live" in screen._submit_btn.text().lower()


def test_run_only_submits_approved_in_dry_run(
    qtbot, workdir, worker, settings, stub_batch, monkeypatch
):
    # Confirmation dialog returns Yes without showing UI.
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.Yes),
    )
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    # Only select Job A (not Job C). Job B is non-ready, untickable.
    screen._row_checkboxes[0].setChecked(True)
    qtbot.wait(20)
    with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
        screen._on_submit_clicked()

    # The synthetic run was called with exactly the approved set.
    assert len(stub_batch["run_calls"]) == 1
    call = stub_batch["run_calls"][0]
    assert call["urls"] == ["https://au.seek.com/job/A"]
    assert call["allow_real_submit"] is False
    # The "live submit" sentinel was never reached.
    assert stub_batch["submit_invoked"] is False


def test_stop_halts_batch_between_jobs(
    qtbot, workdir, worker, settings, stub_batch, monkeypatch
):
    """STOP sets the worker's stop-batch event and the synthetic run_batch
    honors it between iterations. We verify by approving 3 jobs (only 2 are
    'ready' so we have to tick all readies + force-tick the non-ready row
    by going through a longer stub). Easier: replace the stub to make each
    job sleep so we can stop after the first."""

    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.Yes),
    )

    # Override the run stub with a slower one that lets us stop mid-batch.
    seen_jobs: list[str] = []

    async def slow_run(*, job_urls, engine_workdir, allow_real_submit,
                       on_progress, is_cancelled, is_stopped,
                       throttle_range_seconds=(0, 0), tally=None, **_kw):
        # Updated for D's Contract 5: tally is owned by the caller and
        # mutated in place; throttle is a (min, max) tuple now.
        if tally is None:
            tally = BatchRunResult()
        for i, url in enumerate(job_urls, 1):
            if is_stopped():
                tally.stop_reason = "user_stop"
                return tally
            seen_jobs.append(url)
            result = ApplicationResult(
                job_url=url, status=ApplicationStatus.DRY_RUN_VERIFIED,
            )
            tally.per_job.append(result)
            tally.dry_run_verified += 1
            on_progress(i, len(job_urls), result)
            # Sleep briefly so the test can call stop() between iterations.
            await asyncio.sleep(0.1)
        return tally

    monkeypatch.setattr(worker_module, "run_batch", slow_run)

    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    screen._on_select_all_clicked()
    qtbot.wait(20)

    with qtbot.waitSignal(worker.batch_apply_progress, timeout=3000):
        screen._on_submit_clicked()
    # Now we have processed the first job. Stop the batch.
    screen._on_stop_clicked()

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=3000):
        pass

    # Exactly 1 job processed (the first one) before stop took effect.
    assert len(seen_jobs) == 1
    assert seen_jobs[0] == "https://au.seek.com/job/A"


def test_failed_and_not_quick_apply_rows_tallied_and_not_retried(
    qtbot, workdir, worker, settings, monkeypatch
):
    """run_batch (real) iterates urls once each. Verify no retry happens by
    counting how many times the synthetic apply_to_job is called per URL."""
    import autoapply_next.engine.batch as batch_module

    calls: dict[str, int] = {}

    async def fake_apply(*, job_url, engine_workdir, on_progress,
                          is_cancelled, allow_real_submit, match_threshold,
                          screenshot_dir=None):
        calls[job_url] = calls.get(job_url, 0) + 1
        # First URL: succeeds (DRY_RUN_VERIFIED). Second: fails.
        if job_url == "https://au.seek.com/job/PASS":
            return ApplicationResult(
                job_url=job_url, status=ApplicationStatus.DRY_RUN_VERIFIED,
            )
        return ApplicationResult(
            job_url=job_url, status=ApplicationStatus.FAILED,
            error_message="boom",
            exception_type="RuntimeError",
        )

    monkeypatch.setattr(batch_module, "apply_to_job", fake_apply)

    async def run():
        from autoapply_next.engine.batch import run_batch

        return await run_batch(
            job_urls=[
                "https://au.seek.com/job/PASS",
                "https://au.seek.com/job/FAIL1",
                "https://au.seek.com/job/FAIL2",
            ],
            engine_workdir=workdir,
            allow_real_submit=False,
            on_progress=None,
            is_cancelled=lambda: False,
            is_stopped=lambda: False,
            throttle_range_seconds=(0, 0),
            # Disable D's consecutive-failure circuit breaker so this test
            # can pin the "every URL attempted once" contract (3 failures
            # in a row would otherwise halt mid-batch).
            max_consecutive_failures=999,
        )

    tally = asyncio.run(run())
    assert tally.dry_run_verified == 1
    assert tally.failed == 2
    # Each URL was attempted exactly once -- no auto-retry.
    assert calls == {
        "https://au.seek.com/job/PASS": 1,
        "https://au.seek.com/job/FAIL1": 1,
        "https://au.seek.com/job/FAIL2": 1,
    }
    assert tally.stop_reason == "completed"


def test_run_with_live_submit_gate_requires_confirmation(
    qtbot, workdir, worker, settings, stub_batch, monkeypatch
):
    """If the user cancels the LIVE confirmation, the worker is NOT called."""
    settings.allow_real_submit = True
    qtbot.wait(20)

    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.Cancel),
    )

    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings
    )
    qtbot.addWidget(screen)
    with qtbot.waitSignal(worker.batch_prepare_finished, timeout=3000):
        screen._on_prepare_clicked()
    screen._on_select_all_clicked()
    qtbot.wait(20)
    screen._on_submit_clicked()
    qtbot.wait(50)
    assert stub_batch["run_calls"] == [], (
        "Cancel on LIVE confirmation must not call run_batch"
    )
    # And the sentinel for the real engine path is also clean.
    assert stub_batch["submit_invoked"] is False


# --------------------------------------------------------------- prepare DB integration


def _seed_db(workdir: Path, rows: list[tuple]) -> None:
    db = workdir / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, timestamp TEXT, "
            "failure_count INTEGER)"
        )
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, timestamp, failure_count) "
                "VALUES (?,?,?,?,?,?,?,0)",
                r,
            )


def test_prepare_reads_db_threshold_and_status_queued(workdir):
    """The DB read inside prepare_batch must filter by status='queued' AND
    match_score >= threshold. Confirms the SQL contract."""
    _seed_db(
        workdir,
        [
            ("https://au.seek.com/job/H", "High", "Acme", "seek", 80, "queued", "t1"),
            ("https://au.seek.com/job/L", "Low",  "Bee",  "seek",  5, "queued", "t2"),
            ("https://au.seek.com/job/A", "App",  "Cee",  "seek", 90, "applied","t3"),
        ],
    )
    from autoapply_next.engine.batch import _queued_jobs_from_db

    rows = _queued_jobs_from_db(workdir, min_score=10)
    urls = [r[0] for r in rows]
    assert "https://au.seek.com/job/H" in urls
    assert "https://au.seek.com/job/L" not in urls  # below threshold
    assert "https://au.seek.com/job/A" not in urls  # already applied
