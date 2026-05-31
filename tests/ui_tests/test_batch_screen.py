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


# The six tests that used to live here exercised the manual Prepare /
# Select-all / Deselect-all / Submit-N-dry-run / Submit-N-LIVE-confirm
# flow on BatchScreen. That UI was removed when the user asked for
# auto-apply on scrape (no more per-job review tick, no more Submit
# button); BatchScreen is now a status-only monitor and the trigger
# lives on QueueScreen's "Scrape and apply" button. The replacement
# tests below cover the new contract: the worker's chained runner, the
# BatchScreen status table populating from batch_apply_progress, and
# the STOP-via-auto-apply path.


def test_scrape_and_auto_apply_chains_phase0_scrape_phase2(
    qtbot, workdir, worker, monkeypatch
):
    """The new chained runner mirrors job-finder's order:
    Phase 0 (apply queued FIRST) -> Phase 1 (scrape) -> Phase 2 (apply new).

    Job-finder explicitly clears the queue before scraping; this test
    pins that order. Because the stub doesn't mutate jobs.db, the same
    queued row is visible in BOTH Phase 0 and Phase 2 eligibility
    checks; we assert run_batch is called twice and the URL appears
    twice (once per phase). In production, persist_apply_outcome would
    flip the row to 'applied' after Phase 0 so Phase 2 would not see
    it; that integration is covered by test_integration_resilience.py.
    """
    import sqlite3

    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
        for url, score in [
            ("https://au.seek.com/job/HI", 80),
            ("https://au.seek.com/job/LO", 5),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, timestamp, "
                " failure_count) VALUES (?,?,?,?,?,?,?,0)",
                (url, "t", "c", "seek", score, "queued", "t"),
            )

    order: list[str] = []  # 'phase 0 run', 'scrape', 'phase 2 run'

    async def fake_scrape(**_kw):
        order.append("scrape")
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    run_calls: list[list[str]] = []

    async def fake_run(*, job_urls, tally=None, **_kw):
        run_calls.append(list(job_urls))
        order.append(f"run({len(job_urls)})")
        if tally is None:
            tally = BatchRunResult()
        for u in job_urls:
            tally.per_job.append(ApplicationResult(
                job_url=u, status=ApplicationStatus.DRY_RUN_VERIFIED,
            ))
            tally.dry_run_verified += 1
        tally.stop_reason = "completed"
        return tally

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=False, throttle_seconds=0,
        )

    # The order MUST be Phase 0 run -> scrape -> Phase 2 run, never
    # scrape -> run.
    assert order[0].startswith("run("), order
    assert "scrape" in order
    assert order.index("scrape") > 0, (
        f"scrape ran before Phase 0; order={order}"
    )
    # HI is eligible (score 80 >= 10), LO is not (score 5 < 10).
    for call in run_calls:
        assert call == ["https://au.seek.com/job/HI"], call


def test_scrape_skipped_when_phase0_trips_circuit(
    qtbot, workdir, worker, monkeypatch
):
    """If Phase 0 stops for any reason other than 'completed' (fatal,
    consecutive failures, daily cap, user STOP), skip scraping and
    Phase 2 entirely. No point scraping if we cannot apply more."""
    import sqlite3
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
        conn.execute(
            "INSERT INTO applications (url, title, company, board, "
            "match_score, status, timestamp, failure_count) "
            "VALUES (?,?,?,?,?,?,?,0)",
            ("https://au.seek.com/job/X", "t", "c", "seek", 80, "queued", "t"),
        )

    scrape_called = {"n": 0}

    async def fake_scrape(**_kw):
        scrape_called["n"] += 1
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    async def fake_run(*, job_urls, tally=None, **_kw):
        if tally is None:
            tally = BatchRunResult()
        # Simulate a fatal halt mid-Phase-0.
        tally.stop_reason = "fatal:Seek session expired"
        tally.fatal_reason = "Seek session expired"
        return tally

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=3000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=False, throttle_seconds=0,
        )

    assert scrape_called["n"] == 0, (
        "scrape must NOT run after a fatal Phase 0 halt"
    )


def test_scrape_and_auto_apply_no_eligible_jobs_emits_empty_tally(
    qtbot, workdir, worker, monkeypatch
):
    """Phase 1 succeeds but Phase 1.5 finds no queued rows above
    threshold. The worker emits batch_apply_finished with an empty
    BatchRunResult; it does NOT call run_batch."""
    import sqlite3
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )

    async def fake_scrape(**_kw):
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    called = {"run_batch": False}

    async def fake_run(**_kw):
        called["run_batch"] = True
        return BatchRunResult()

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=3000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=False, throttle_seconds=0,
        )

    assert called["run_batch"] is False, (
        "no eligible jobs must NOT call run_batch (avoid empty-batch noise)"
    )


def test_scrape_and_auto_apply_caps_at_max_jobs(
    qtbot, workdir, worker, monkeypatch
):
    """Worker caps the URL list at max_jobs (default MAX_APPLIES_PER_RUN
    from job-finder = 100). Smaller cap passed here for fast tests."""
    import sqlite3
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
        for i in range(7):
            conn.execute(
                "INSERT INTO applications (url, title, company, board, "
                "match_score, status, timestamp, failure_count) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (f"https://au.seek.com/job/{i}", "t", "c", "seek",
                 50, "queued", f"t{i}"),
            )

    async def fake_scrape(**_kw):
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    seen: list[list[str]] = []

    async def fake_run(*, job_urls, tally=None, **_kw):
        seen.append(list(job_urls))
        if tally is None:
            tally = BatchRunResult()
        return tally

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=False, throttle_seconds=0,
            max_jobs=3,  # cap below the 7 eligible
        )

    assert seen and len(seen[0]) == 3


def test_auto_apply_passes_gate_through_to_run_batch(
    qtbot, workdir, worker, monkeypatch
):
    """The Settings allow_real_submit flag flows verbatim to run_batch.
    LIVE on the gate -> allow_real_submit=True. No extra confirmation
    inside the worker (the Settings flip is the single confirmation)."""
    import sqlite3
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
        conn.execute(
            "INSERT INTO applications (url, title, company, board, "
            "match_score, status, timestamp, failure_count) "
            "VALUES (?,?,?,?,?,?,?,0)",
            ("https://au.seek.com/job/X", "t", "c", "seek", 80, "queued", "t"),
        )

    async def fake_scrape(**_kw):
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    captured = {"allow_real_submit": None}

    async def fake_run(*, allow_real_submit, tally=None, **_kw):
        captured["allow_real_submit"] = allow_real_submit
        if tally is None:
            tally = BatchRunResult()
        return tally

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", fake_run)

    with qtbot.waitSignal(worker.batch_apply_finished, timeout=3000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=True, throttle_seconds=0,
        )

    assert captured["allow_real_submit"] is True


def test_stop_halts_auto_apply_between_jobs(
    qtbot, workdir, worker, monkeypatch
):
    """STOP set after the first per-job result lands; the runner's
    is_stopped() flag halts before the next URL. Remaining URLs stay
    'queued' in jobs.db (proven by D's contract; here we just assert
    the stub did not receive the third URL)."""
    import sqlite3
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
        for i, url in enumerate(["A", "B", "C"]):
            conn.execute(
                "INSERT INTO applications (url, title, company, board, "
                "match_score, status, timestamp, failure_count) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (f"https://au.seek.com/job/{url}", "t", "c", "seek",
                 80, "queued", f"t{i}"),
            )

    async def fake_scrape(**_kw):
        from autoapply_next.engine.scraping import ScrapeResult
        return ScrapeResult(
            keyword="kw", total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    seen: list[str] = []

    async def slow_run(*, job_urls, on_progress, is_stopped,
                       tally=None, **_kw):
        if tally is None:
            tally = BatchRunResult()
        for i, url in enumerate(job_urls, 1):
            if is_stopped():
                tally.stop_reason = "user_stop"
                return tally
            seen.append(url)
            result = ApplicationResult(
                job_url=url, status=ApplicationStatus.DRY_RUN_VERIFIED,
            )
            tally.per_job.append(result)
            tally.dry_run_verified += 1
            on_progress(i, len(job_urls), result)
            await asyncio.sleep(0.1)
        return tally

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)
    monkeypatch.setattr(worker_module, "run_batch", slow_run)

    finished: list[BatchRunResult] = []
    worker.batch_apply_finished.connect(finished.append)

    # Kick off auto-apply.
    with qtbot.waitSignal(worker.batch_apply_progress, timeout=3000):
        worker.scrape_and_auto_apply(
            "kw", allow_real_submit=False, throttle_seconds=0,
        )
    # First per-job result has landed; call STOP.
    worker.stop_batch()
    with qtbot.waitSignal(worker.batch_apply_finished, timeout=3000):
        pass

    assert len(seen) == 1, f"only the first URL should have been seen; got {seen}"
    assert finished[0].stop_reason == "user_stop"


def test_batch_screen_table_populates_from_apply_progress(
    qtbot, workdir, worker, settings
):
    """BatchScreen no longer has Prepare/Submit/Select-all/Deselect-all.
    Its table now populates from batch_apply_progress events directly."""
    screen = BatchScreen(
        engine_workdir=workdir, worker=worker, settings=settings,
    )
    qtbot.addWidget(screen)
    # No row checkboxes attribute on the new screen; assert removal.
    assert not hasattr(screen, "_row_checkboxes")
    assert not hasattr(screen, "_submit_btn")
    assert not hasattr(screen, "_prepare_btn")
    assert not hasattr(screen, "_select_all_btn")
    # STOP button is present and disabled while idle.
    assert screen._stop_btn.text() == "STOP batch"
    assert screen._stop_btn.isEnabled() is False
    # Fire two batch_apply_progress events; assert 2 rows show up.
    worker.batch_apply_progress.emit(
        1, 2,
        ApplicationResult(job_url="https://au.seek.com/job/1",
                          status=ApplicationStatus.DRY_RUN_VERIFIED),
    )
    worker.batch_apply_progress.emit(
        2, 2,
        ApplicationResult(job_url="https://au.seek.com/job/2",
                          status=ApplicationStatus.FAILED,
                          error_message="boom",
                          exception_type="RuntimeError"),
    )
    qtbot.wait(50)
    assert screen._table.rowCount() == 2


def test_queue_emits_auto_apply_started_signal(
    qtbot, workdir, worker, settings, monkeypatch
):
    """QueueScreen.run_clicked must now (a) call worker.scrape_and_auto_apply
    not the old scrape_and_score, and (b) emit auto_apply_started so
    MainWindow can swap to the Batch screen."""
    from autoapply_next.ui.queue_screen import QueueScreen

    called = {"scrape_and_auto_apply": 0, "scrape_and_score": 0}

    def fake_auto(self, kw, *, allow_real_submit, throttle_seconds=0,
                  daily_cap=0, **_kw):
        called["scrape_and_auto_apply"] += 1

    def fake_score(self, kw, location="Australia"):
        called["scrape_and_score"] += 1

    monkeypatch.setattr(EngineWorker, "scrape_and_auto_apply", fake_auto)
    monkeypatch.setattr(EngineWorker, "scrape_and_score", fake_score)

    screen = QueueScreen(
        engine_workdir=workdir, worker=worker, settings=settings,
    )
    qtbot.addWidget(screen)
    screen._keyword_input.setText("aws")
    started: list[bool] = []
    screen.auto_apply_started.connect(lambda: started.append(True))
    screen._on_refresh_clicked()
    qtbot.wait(30)
    assert called["scrape_and_auto_apply"] == 1
    assert called["scrape_and_score"] == 0
    assert started == [True]


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


# test_run_with_live_submit_gate_requires_confirmation removed.
# The per-batch LIVE-submit confirmation lived on the Submit button which
# no longer exists (auto-apply flow). The single confirmation now lives on
# the Settings checkbox itself, covered by
# tests/ui_tests/test_interaction_audit.py::test_settings_real_submit_cancel_does_not_flip
# and test_settings_real_submit_confirm_flips_and_back_off_without_prompt.


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
