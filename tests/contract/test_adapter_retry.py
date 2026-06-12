"""Contract tests for `apply_to_job` retry + persist-check logic.

These tests verify the bounded-retry + in_progress-write + persist-check
contract without touching real Seek, real Claude, or real LibreOffice.

Strategy: mirror `test_safety_gate.py` by injecting synthetic `seek_apply`,
`applicator`, `matcher`, `tailorer`, and `models` modules into sys.modules.
The adapter imports them lazily inside `_engine_workdir`, so each test
controls what those imports return.

Key contracts under test:

- PEEK / SCORE / TAILOR are retried up to MAX_RETRIES with exponential
  backoff RETRY_DELAY * attempt.
- APPLY is NEVER retried (one-shot per the hard constraint).
- CoverLetterQualityError in TAILOR is NOT retried (structural / same
  input means same output).
- is_fatal_condition matches refuse retry and return FAILED immediately.
- `persist_in_progress` is written BEFORE applicator.apply ONLY when
  `allow_real_submit=True`. Dry-run must not leave the row in_progress.
- Every persist_apply_outcome call is checked. If `written=False` and the
  status was SUBMITTED, the result is downgraded to SUBMITTED_UNCERTAIN.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest


# ----------------------------------------------------------------- helpers


def _make_db(workdir: Path) -> None:
    """Create a minimal jobs.db with the engine's `applications` schema."""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )


def _stage_workdir(tmp_path: Path) -> Path:
    """Build a runnable engine_workdir: config.yaml + sessions/ + jobs.db."""
    (tmp_path / "config.yaml").write_text(
        "candidate:\n"
        "  name: Test User\n"
        "  email: test@example.com\n"
        "  phone: '+61 400 000 000'\n",
        encoding="utf-8",
    )
    (tmp_path / "sessions" / "seek").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "seek" / "state.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "errors").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)
    _make_db(tmp_path)
    return tmp_path


@dataclass
class FakeJobListing:
    url: str
    title: str
    company: str
    board: str = "seek"
    description: str = ""
    posted_at: str = ""
    easy_apply: bool = True


class _SeekApplyError(Exception):
    pass


class _CoverLetterQualityError(Exception):
    pass


class _BoardBlockedError(Exception):
    pass


@dataclass
class _Counters:
    """Mutable side channel test fixtures use to count stage calls."""
    peek_calls: int = 0
    score_calls: int = 0
    tailor_calls: int = 0
    apply_calls: int = 0
    persist_in_progress_calls: int = 0
    persist_apply_outcome_calls: list = field(default_factory=list)


@pytest.fixture
def engine_modules(tmp_path: Path, monkeypatch):
    """Install synthetic engine modules into sys.modules for one test.

    The test customises the per-stage behaviours by overwriting attributes
    on `seek_apply` / `matcher` / `tailorer` / `applicator` after the
    fixture returns. The counters object captures call counts.
    """
    workdir = _stage_workdir(tmp_path)
    counters = _Counters()

    # --- seek_apply: peek_is_quick_apply + fetch_seek_jd ----------------
    seek_apply = types.ModuleType("seek_apply")
    seek_apply.SeekApplyError = _SeekApplyError

    async def default_peek_is_quick_apply(url, session_state):
        return True, {}

    async def default_fetch_seek_jd(url, session_state):
        return "Job description text"

    async def default_submit(page):
        pass

    async def default_tick(page):
        pass

    async def default_verify_applied(page, title, company):
        return True

    async def default_scrape_applied(page):
        return []

    seek_apply.peek_is_quick_apply = default_peek_is_quick_apply
    seek_apply.fetch_seek_jd = default_fetch_seek_jd
    seek_apply._submit = default_submit
    seek_apply._tick_terms_checkbox = default_tick
    seek_apply._verify_applied = default_verify_applied
    seek_apply._scrape_applied_cards = default_scrape_applied

    class _Journal:
        _data: dict = {}

    seek_apply._Journal = _Journal
    sys.modules["seek_apply"] = seek_apply

    # --- models: JobListing --------------------------------------------
    models = types.ModuleType("models")
    models.JobListing = FakeJobListing
    sys.modules["models"] = models

    # --- matcher.score_job ---------------------------------------------
    matcher = types.ModuleType("matcher")

    async def default_score_job(job):
        counters.score_calls += 1
        return 80, "Strong match"

    matcher.score_job = default_score_job
    sys.modules["matcher"] = matcher

    # --- tailorer.tailor + CoverLetterQualityError ---------------------
    tailorer = types.ModuleType("tailorer")
    tailorer.CoverLetterQualityError = _CoverLetterQualityError

    resume_pdf = workdir / "output" / "resume.pdf"
    cover_pdf = workdir / "output" / "cover.pdf"
    # Ensure parent exists; tests do not actually write the PDFs but the
    # adapter writes a sidecar .txt next to cover_pdf.
    resume_pdf.parent.mkdir(parents=True, exist_ok=True)

    async def default_tailor(job, tier="full"):
        counters.tailor_calls += 1
        return str(resume_pdf), str(cover_pdf)

    def default_export_cover_letter_pdf(cover_text, job):
        return str(cover_pdf)

    tailorer.tailor = default_tailor
    tailorer._export_cover_letter_pdf = default_export_cover_letter_pdf
    sys.modules["tailorer"] = tailorer

    # --- applicator.apply + BoardBlockedError --------------------------
    applicator = types.ModuleType("applicator")
    applicator.BoardBlockedError = _BoardBlockedError

    async def default_apply(job, resume_pdf, cover_pdf, candidate, page=None):
        # `page=None` mirrors applicator.apply's signature after
        # autoapply-next started threading the peek page through to
        # avoid leaking a tab per job (job-finder parity).
        counters.apply_calls += 1

    applicator.apply = default_apply
    sys.modules["applicator"] = applicator

    # ----- patch the peek path the adapter actually calls ----------
    # The adapter's `_peek_and_fetch_listing` calls
    # `seek_apply.peek_is_quick_apply` and `seek_apply.fetch_seek_jd`
    # directly. We override the default factories below so each test can
    # introspect / sabotage them. The `peek_calls` counter lives here so
    # both helpers bump the same number.

    from autoapply_next.engine import adapter as adapter_mod

    original_peek_and_fetch = adapter_mod._peek_and_fetch_listing

    async def counting_peek(job_url):
        counters.peek_calls += 1
        return await original_peek_and_fetch(job_url)

    monkeypatch.setattr(adapter_mod, "_peek_and_fetch_listing", counting_peek)

    yield workdir, counters

    # ---- teardown: evict our synthetic modules ------------------------
    for name in (
        "seek_apply",
        "models",
        "matcher",
        "tailorer",
        "applicator",
    ):
        sys.modules.pop(name, None)


def _fast_sleep_patch(monkeypatch):
    """Replace `asyncio.sleep` *as seen by the adapter module* with a
    near-no-op so the inter-attempt backoff doesn't make tests slow.

    We bind the original `asyncio.sleep` once and use that inside the
    replacement; replacing the global asyncio.sleep would recurse forever
    because the adapter calls `asyncio.sleep` via the same name we just
    replaced. We patch `adapter_mod.asyncio` to a SimpleNamespace exposing
    the same names the adapter uses, with `sleep` swapped.
    """
    from autoapply_next.engine import adapter as adapter_mod

    real_sleep = asyncio.sleep

    async def quick_sleep(seconds):
        # Yield control so cancellation can still propagate, but don't
        # actually wait. Ignore the duration argument.
        await real_sleep(0)

    fake_asyncio = types.SimpleNamespace(
        sleep=quick_sleep,
        CancelledError=asyncio.CancelledError,
    )
    monkeypatch.setattr(adapter_mod, "asyncio", fake_asyncio)


def _stub_persist_in_progress(monkeypatch, counters: _Counters):
    """Replace adapter.persist_in_progress with a counter-bumping stub
    that returns a successful PersistResult. Tests use this to assert
    that the adapter wrote in_progress before applicator.apply (or did
    not, on dry-run)."""
    from autoapply_next.engine import adapter as adapter_mod
    from autoapply_next.engine.persistence import PersistResult

    def stub(*, engine_workdir, url, title="", company="", score=None):
        counters.persist_in_progress_calls += 1
        return PersistResult(written=True, status="in_progress")

    monkeypatch.setattr(adapter_mod, "persist_in_progress", stub)


def _stub_persist_apply_outcome(monkeypatch, counters: _Counters):
    """Replace adapter.persist_apply_outcome with a counter that captures
    the result and returns success."""
    from autoapply_next.engine import adapter as adapter_mod
    from autoapply_next.engine.persistence import PersistResult, map_status

    def stub(*, engine_workdir, result):
        new_status = map_status(result)
        counters.persist_apply_outcome_calls.append(
            (result.status.value, new_status)
        )
        return PersistResult(written=True, status=new_status)

    monkeypatch.setattr(adapter_mod, "persist_apply_outcome", stub)


# =================================================================== tests


def test_peek_retries_twice_then_succeeds(engine_modules, monkeypatch):
    """Two transient network errors, then a successful peek. The adapter
    returns success and the peek stage was hit 3 times (1 fail + 1 fail
    + 1 ok). Score / tailor / apply each ran once."""
    from autoapply_next.engine.adapter import apply_to_job

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    # Make the PEEK stage fail twice with a transient network error
    # then succeed. `_peek_and_fetch_listing` calls fetch_seek_jd FIRST,
    # then peek_is_quick_apply. Sabotaging fetch_seek_jd is sufficient
    # to fail the stage on the first two attempts.
    import seek_apply  # the stub installed by the fixture

    real_fetch = seek_apply.fetch_seek_jd
    fail_calls = {"n": 0}

    async def flaky_fetch(url, session_state):
        fail_calls["n"] += 1
        if fail_calls["n"] <= 2:
            raise ConnectionError(f"network blip #{fail_calls['n']}")
        return await real_fetch(url, session_state)

    seek_apply.fetch_seek_jd = flaky_fetch

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/100",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    # Final stage was apply -> SUBMITTED.
    from autoapply_next.engine.results import ApplicationStatus

    assert result.status == ApplicationStatus.SUBMITTED, (
        f"unexpected status {result.status}, error={result.error_message}"
    )
    # peek_calls increments inside _peek_and_fetch_listing wrapper. It is
    # called 3 times (2 transient failures, 1 success).
    assert counters.peek_calls == 3, (
        f"peek_calls={counters.peek_calls}, expected 3"
    )
    assert counters.score_calls == 1
    assert counters.tailor_calls == 1
    assert counters.apply_calls == 1


def test_score_retries_three_times_all_fail(engine_modules, monkeypatch):
    """Three score failures in a row. Adapter returns FAILED with the
    last exception in error_message. apply_calls == 0."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    import matcher

    async def always_fail(job):
        counters.score_calls += 1
        raise RuntimeError(f"claude transient #{counters.score_calls}")

    matcher.score_job = always_fail

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/200",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert result.status == ApplicationStatus.FAILED
    # Three attempts, exhausted.
    assert counters.score_calls == 3, (
        f"score_calls={counters.score_calls}, expected 3"
    )
    # apply was never reached.
    assert counters.apply_calls == 0
    # The final-attempt error message bubbled up.
    assert "claude transient" in (result.error_message or "")


def test_tailor_quality_error_not_retried(engine_modules, monkeypatch):
    """CoverLetterQualityError is structural; same JD -> same refusal.
    Adapter must NOT retry. The adapter returns FAILED after a SINGLE
    tailor call; persistence will map it to 'skipped' via map_status."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    import tailorer

    async def quality_refusal(job, tier="full"):
        counters.tailor_calls += 1
        raise tailorer.CoverLetterQualityError("Claude refused to tailor")

    tailorer.tailor = quality_refusal

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/300",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert result.status == ApplicationStatus.FAILED
    assert result.exception_type == "_CoverLetterQualityError"
    # Critical: exactly one attempt, no retry.
    assert counters.tailor_calls == 1, (
        f"tailor_calls={counters.tailor_calls}, expected 1 (no retry)"
    )
    assert counters.apply_calls == 0


def test_fatal_condition_in_tailor_not_retried(engine_modules, monkeypatch):
    """A PermissionError (session not loaded) is is_fatal_condition-fatal.
    Adapter returns FAILED on first attempt, no retry, no apply."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    import tailorer

    async def session_not_loaded(job, tier="full"):
        counters.tailor_calls += 1
        raise PermissionError("Seek session state.json missing")

    tailorer.tailor = session_not_loaded

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/350",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert result.status == ApplicationStatus.FAILED
    # Single attempt; the fatal classifier refused retry.
    assert counters.tailor_calls == 1
    # The fatal reason was prepended to error_message.
    assert "fatal:" in (result.error_message or "")
    assert "session not loaded" in (result.error_message or "").lower()
    assert counters.apply_calls == 0


def test_submitted_path_writes_in_progress_then_applied(
    engine_modules, monkeypatch
):
    """Happy path with allow_real_submit=True. The adapter:
       1. writes in_progress BEFORE applicator.apply
       2. writes applied AFTER applicator.apply returns
    The order matters: in_progress must be written first so a crash
    mid-apply is recoverable."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)

    # Track the SEQUENCE of persist calls (in_progress vs apply_outcome).
    sequence: list[str] = []

    from autoapply_next.engine import adapter as adapter_mod
    from autoapply_next.engine.persistence import PersistResult, map_status

    def stub_in_progress(*, engine_workdir, url, title="", company="", score=None):
        sequence.append("in_progress")
        counters.persist_in_progress_calls += 1
        return PersistResult(written=True, status="in_progress")

    def stub_apply_outcome(*, engine_workdir, result):
        sequence.append(f"outcome:{result.status.value}")
        counters.persist_apply_outcome_calls.append(
            (result.status.value, map_status(result))
        )
        return PersistResult(written=True, status=map_status(result))

    monkeypatch.setattr(adapter_mod, "persist_in_progress", stub_in_progress)
    monkeypatch.setattr(
        adapter_mod, "persist_apply_outcome", stub_apply_outcome
    )

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/400",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert result.status == ApplicationStatus.SUBMITTED
    assert counters.persist_in_progress_calls == 1
    # Two persist calls: in_progress, then SUBMITTED outcome.
    assert sequence == ["in_progress", "outcome:submitted"], sequence


def test_dry_run_does_not_write_in_progress(engine_modules, monkeypatch):
    """With allow_real_submit=False (the default), the adapter must NOT
    write in_progress. The dry-run path raises DryRunReached inside
    applicator.apply and returns DRY_RUN_VERIFIED; if we wrote in_progress,
    Workstream A's recover_orphans would force-fail the row on next
    startup, which is wrong since no real submit happened."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    # Force a DryRunReached out of applicator.apply, mirroring what the
    # SafetyGate does in production when allow_real_submit=False.
    import applicator
    from autoapply_next.engine.safety import DryRunReached

    async def dry_run_apply(job, resume_pdf, cover_pdf, candidate, page=None):
        counters.apply_calls += 1
        raise DryRunReached(
            screenshot_path=None,
            submit_button_text="Submit application",
        )

    applicator.apply = dry_run_apply

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/500",
            engine_workdir=workdir,
            allow_real_submit=False,
        )
    )

    assert result.status == ApplicationStatus.DRY_RUN_VERIFIED
    # The hard contract: no in_progress write on dry-run.
    assert counters.persist_in_progress_calls == 0, (
        "Dry-run must NOT write in_progress; recover_orphans would then "
        "force-fail a row that never submitted."
    )
    # applicator.apply DID run (the gate fires inside it).
    assert counters.apply_calls == 1
    # And no apply-outcome persistence either (DRY_RUN_VERIFIED is a no-op).
    assert counters.persist_apply_outcome_calls == [], (
        counters.persist_apply_outcome_calls
    )


def test_persist_write_failure_downgrades_submitted_to_uncertain(
    engine_modules, monkeypatch
):
    """The engine submitted; the verifier confirmed; but the DB write
    failed. We don't know what subsequent eligibility sees, so the
    in-memory result is downgraded from SUBMITTED to SUBMITTED_UNCERTAIN
    with a 'Persist failed:' error message."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)

    # Force persist_apply_outcome to claim a write failure.
    from autoapply_next.engine import adapter as adapter_mod
    from autoapply_next.engine.persistence import PersistResult

    def failing_persist(*, engine_workdir, result):
        counters.persist_apply_outcome_calls.append(result.status.value)
        return PersistResult(
            written=False,
            status=None,
            error="disk full",
        )

    monkeypatch.setattr(adapter_mod, "persist_apply_outcome", failing_persist)

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/600",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    # SUBMITTED was downgraded to SUBMITTED_UNCERTAIN.
    assert result.status == ApplicationStatus.SUBMITTED_UNCERTAIN, (
        f"expected downgrade to SUBMITTED_UNCERTAIN, got {result.status} "
        f"error={result.error_message}"
    )
    assert "Persist failed" in (result.error_message or "")
    assert "disk full" in (result.error_message or "")
    # The failing persist was attempted with the SUBMITTED status.
    assert counters.persist_apply_outcome_calls == ["submitted"]


def test_apply_stage_is_not_retried(engine_modules, monkeypatch):
    """Submit + verify is one-shot per the hard constraint. A
    SeekApplyError out of applicator.apply must NOT cause a retry; the
    adapter returns FAILED immediately. The whole point: never auto-retry
    submit (would risk duplicate applications)."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules
    _fast_sleep_patch(monkeypatch)
    _stub_persist_in_progress(monkeypatch, counters)
    _stub_persist_apply_outcome(monkeypatch, counters)

    import applicator
    import seek_apply

    async def failing_apply(job, resume_pdf, cover_pdf, candidate, page=None):
        counters.apply_calls += 1
        raise seek_apply.SeekApplyError("submit stuck on step 5")

    applicator.apply = failing_apply

    result = asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/700",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert result.status == ApplicationStatus.FAILED
    # The critical assertion: exactly one apply attempt, never retried.
    assert counters.apply_calls == 1, (
        f"apply_calls={counters.apply_calls}, expected 1 (apply is one-shot)"
    )


# ============================================================================
# peek-page-threading contract (regression: visible-tab-leak bug)
#
# Symptom observed live: chromium showed new tabs accumulating as the batch
# ran. Root cause: `_peek_and_fetch_listing` discarded the open Page returned
# by `seek_apply.peek_is_quick_apply`, and `applicator.apply` was called with
# no `page=` kwarg, so seek_apply.apply_seek_quick opened a brand-new tab.
# The orphaned peek tab lived in the engine's persistent context until the
# whole context was torn down between phases. Per job: 1 leaked tab.
#
# The fix threads the peek Page through to applicator.apply, which closes
# it in its finally (job-finder parity, main.py:101-140).


def test_peek_page_is_threaded_into_applicator_apply(
    engine_modules, monkeypatch
):
    """When peek_is_quick_apply returns a Page object, apply_to_job must
    pass that exact object to applicator.apply via the `page=` kwarg, so
    seek_apply.apply_seek_quick reuses (and ultimately closes) it instead
    of opening a fresh tab."""
    from autoapply_next.engine.adapter import apply_to_job

    workdir, counters = engine_modules

    class _FakePage:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake_page = _FakePage()

    import seek_apply

    async def peek_returns_page(url, session_state):
        # The shape the real seek_apply.peek_is_quick_apply uses on the
        # quick-apply path: (True, <live Page>). The adapter must forward
        # this page object all the way into applicator.apply.
        return True, fake_page

    seek_apply.peek_is_quick_apply = peek_returns_page

    import applicator

    seen_page: dict = {}

    async def assert_apply(job, resume_pdf, cover_pdf, candidate, page=None):
        seen_page["page"] = page
        counters.apply_calls += 1

    applicator.apply = assert_apply

    asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/800",
            engine_workdir=workdir,
            allow_real_submit=True,
        )
    )

    assert counters.apply_calls == 1
    assert seen_page.get("page") is fake_page, (
        f"applicator.apply received page={seen_page.get('page')!r}, "
        "expected the peek Page object (the visible-tab-leak regression)."
    )


def test_peek_page_is_closed_when_score_below_threshold(
    engine_modules, monkeypatch
):
    """If the job is skipped (score below threshold) before applicator.apply
    runs, the adapter owns the close on the peek Page. Otherwise the page
    leaks: every below-threshold job would add a stale tab to the
    persistent context."""
    from autoapply_next.engine.adapter import apply_to_job

    workdir, counters = engine_modules

    class _FakePage:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake_page = _FakePage()

    import seek_apply

    async def peek_returns_page(url, session_state):
        return True, fake_page

    seek_apply.peek_is_quick_apply = peek_returns_page

    # Force the score below the default threshold of 20.
    import matcher

    async def low_score(job):
        return 5, "weak match"

    matcher.score_job = low_score

    asyncio.run(
        apply_to_job(
            job_url="https://au.seek.com/job/801",
            engine_workdir=workdir,
            allow_real_submit=True,
            match_threshold=20,
        )
    )

    # Apply was never called (skipped on score), and the adapter closed
    # the peek page itself.
    assert counters.apply_calls == 0
    assert fake_page.closed is True, (
        "Peek page was leaked on the score-below-threshold path; "
        "_close_open_page_safely was not called."
    )


def test_same_role_duplicate_skips_before_score_and_apply(
    engine_modules, monkeypatch
):
    """A queued listing whose employer+title role was already applied to
    under a different url must be short-circuited to a 'skipped' duplicate
    BEFORE any score / tailor / submit. This is the fix for the same-role
    duplicate harm (133 real duplicates found in the production db)."""
    from autoapply_next.engine.adapter import apply_to_job
    from autoapply_next.engine.persistence import map_status
    from autoapply_next.engine.results import ApplicationStatus

    workdir, counters = engine_modules

    current = "https://au.seek.com/job/2"
    sibling = "https://au.seek.com/job/1"
    with sqlite3.connect(workdir / "jobs.db") as conn:
        # Sibling listing of the SAME role, already applied to.
        conn.execute(
            "INSERT INTO applications "
            "(url, title, company, board, match_score, status, timestamp, "
            " failure_count) VALUES (?,?,?,?,?,?,?,0)",
            (sibling, "Automation Architect", "Datacom", "seek", 80, "applied", "t"),
        )
        # The current listing the adapter will peek; title/company come from
        # this row via _title_company_from_db.
        conn.execute(
            "INSERT INTO applications "
            "(url, title, company, board, match_score, status, timestamp, "
            " failure_count) VALUES (?,?,?,?,?,?,?,0)",
            (current, "Automation Architect", "Datacom", "seek", 80, "queued", "t"),
        )
        conn.commit()

    result = asyncio.run(
        apply_to_job(
            job_url=current,
            engine_workdir=workdir,
            allow_real_submit=True,
            match_threshold=0,
        )
    )

    assert result.status == ApplicationStatus.FAILED
    assert result.exception_type == "SameRoleDuplicateError"
    assert sibling in (result.error_message or ""), (
        "the skip note should name the sibling url for auditability"
    )
    # The guard fires before scoring, tailoring, and submitting.
    assert counters.score_calls == 0, "scored a known same-role duplicate"
    assert counters.tailor_calls == 0, "tailored a known same-role duplicate"
    assert counters.apply_calls == 0, "SUBMITTED a same-role duplicate"
    # And it persists as 'skipped', so it never re-enters batch eligibility.
    assert map_status(result) == "skipped"


def test_distinct_role_is_not_treated_as_duplicate(engine_modules, monkeypatch):
    """A different role at the same company (or same title at a different
    company) must NOT be skipped: the guard keys on employer+title together."""
    from autoapply_next.engine.adapter import apply_to_job

    workdir, counters = engine_modules

    current = "https://au.seek.com/job/4"
    other = "https://au.seek.com/job/3"
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "INSERT INTO applications "
            "(url, title, company, board, match_score, status, timestamp, "
            " failure_count) VALUES (?,?,?,?,?,?,?,0)",
            (other, "Platform Engineer", "Datacom", "seek", 80, "applied", "t"),
        )
        conn.execute(
            "INSERT INTO applications "
            "(url, title, company, board, match_score, status, timestamp, "
            " failure_count) VALUES (?,?,?,?,?,?,?,0)",
            (current, "Automation Architect", "Datacom", "seek", 80, "queued", "t"),
        )
        conn.commit()

    result = asyncio.run(
        apply_to_job(
            job_url=current,
            engine_workdir=workdir,
            allow_real_submit=True,
            match_threshold=0,
        )
    )

    # A distinct role proceeds through the pipeline and submits.
    assert result.exception_type != "SameRoleDuplicateError"
    assert counters.score_calls == 1
    assert counters.apply_calls == 1
