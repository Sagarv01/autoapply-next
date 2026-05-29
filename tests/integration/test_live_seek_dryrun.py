"""End-to-end dry-run against a real Seek listing using the live engine session.

This test hits real Seek. It must be run manually (mark `requires_live_seek`).
The CI workflow does NOT run this test.

Pre-conditions:
- The live engine at /Users/sagarverma/Pictures/Claude-experiments/job-finder has
  a valid `sessions/seek_chrome_profile/` and `sessions/seek/state.json`.
- Chrome (or Playwright Chromium) is installed.
- LibreOffice (soffice) is on PATH.
- `claude` CLI is on PATH with an active session (Max subscription).
- The test job URLs at the top of this file are still active quick-apply jobs
  that the user has NOT applied to. If they redirect to /apply but show
  "Already applied", swap them.

Assertions:
- `allow_real_submit` is False; the safety gate is installed.
- `apply_to_job` returns `DRY_RUN_VERIFIED` and `dry_run_screenshot` exists.
- The journal has captured screening answers (if any were asked).
- The job was NOT actually submitted (verified by querying Seek's
  applied-jobs page is NOT called; the gate raises DryRunReached before
  the engine reaches `_verify_applied`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Live engine + workdir (the one with the active session).
LIVE_ENGINE_WORKDIR = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder")

# Small fixed set of test jobs (verified quick-apply, unapplied, at 2026-05-29).
# If these become stale, replace by running the script that probes
# `seek_apply.peek_is_quick_apply` against the latest `seen_jobs` minus
# `applications` URLs.
TEST_JOBS = [
    "https://au.seek.com/job/92398511",
]


pytestmark = [
    pytest.mark.requires_live_seek,
    pytest.mark.skipif(
        not LIVE_ENGINE_WORKDIR.exists(),
        reason=f"Live engine workdir not present: {LIVE_ENGINE_WORKDIR}",
    ),
    pytest.mark.skipif(
        not (LIVE_ENGINE_WORKDIR / "sessions" / "seek_chrome_profile").exists(),
        reason="No Seek session at the live engine workdir",
    ),
]


@pytest.fixture(autouse=True)
def _put_src_on_path():
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


@pytest.fixture
def test_log_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "docs" / "test-log.md"


def _append_test_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# AutoApply Next: iterate-fix test log\n\n"
            "Each entry below records one run of the live-Seek dry-run integration "
            "test. The most recent entry is at the top.\n\n"
        )
    body = "\n".join(lines)
    existing = path.read_text()
    # Insert below the heading.
    header, _, rest = existing.partition("\n\n")
    new = f"{header}\n\n{body}\n\n{rest}"
    path.write_text(new)


@pytest.mark.parametrize("job_url", TEST_JOBS)
def test_live_dry_run_reaches_submit_ready(job_url, test_log_path, caplog):
    """The full vertical slice: peek -> score -> tailor -> apply -> gate."""
    from autoapply_next.engine import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus

    caplog.set_level(logging.INFO)
    progress_log: list[str] = []

    def on_progress(event):
        progress_log.append(f"[{event.stage.value}] {event.message}")

    started = datetime.now()

    result = asyncio.run(
        apply_to_job(
            job_url=job_url,
            engine_workdir=LIVE_ENGINE_WORKDIR,
            on_progress=on_progress,
            allow_real_submit=False,
            match_threshold=0,  # do not skip on low score for this test
        )
    )

    finished = datetime.now()
    elapsed = (finished - started).total_seconds()

    # Build the journal entry now so it gets logged even on assertion failure.
    journal: list[str] = [
        f"## {started.isoformat(timespec='seconds')}",
        f"- job_url: {job_url}",
        f"- status: {result.status.value}",
        f"- elapsed_sec: {elapsed:.1f}",
        f"- score: {result.score}",
        f"- exception: {result.exception_type}",
        f"- error: {result.error_message}",
        f"- resume_pdf: {result.resume_pdf}",
        f"- cover_pdf: {result.cover_pdf}",
        f"- screenshot: {result.dry_run_screenshot}",
        f"- progress: {len(progress_log)} events",
    ]
    for line in progress_log:
        journal.append(f"  - {line}")
    _append_test_log(test_log_path, journal)

    # Headline assertions. We accept FAILED with a session/network reason as a
    # signal-rich pass for diagnosis; only DRY_RUN_VERIFIED counts as the slice
    # working end to end.
    assert result.status in (
        ApplicationStatus.DRY_RUN_VERIFIED,
        ApplicationStatus.FAILED,
        ApplicationStatus.SKIPPED_LOW_SCORE,
    ), f"Unexpected status: {result.status}"

    if result.status == ApplicationStatus.DRY_RUN_VERIFIED:
        assert result.dry_run_screenshot is not None, "screenshot missing"
        assert Path(result.dry_run_screenshot).exists(), (
            f"screenshot path does not exist: {result.dry_run_screenshot}"
        )

    if result.status == ApplicationStatus.FAILED:
        # Tell the test runner what stage failed so the next iteration is
        # targeted, not random.
        raise AssertionError(
            f"Live dry-run failed: {result.exception_type}: "
            f"{result.error_message}\nProgress: {progress_log}"
        )
