"""Capstone test: the full self-use loop in DRY_RUN.

Scrape real Seek for a real keyword, pick one good-match job, run
apply_to_job to submit-ready, and confirm the cover letter + Q&A would
render in the Results screen (we read the sidecar files the way the
Results screen does).

Manual-tier test: hits Seek, takes 3 to 5 minutes, requires Claude CLI and
LibreOffice. The CI workflow does NOT run this. Mark with
`requires_live_seek` so it must be explicitly opted into.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

LIVE_ENGINE_WORKDIR = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder")

# A keyword the candidate is strong at AND that the engine has not already
# scraped to exhaustion. The first run with "AWS" returned 212 jobs all
# already seen; "site reliability engineer" is less crowded.
KEYWORD = "site reliability engineer"


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


def _append_test_log(lines: list[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "docs" / "test-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# AutoApply Next: iterate-fix test log\n\n", encoding="utf-8")
    body = "\n".join(lines) + "\n\n"
    existing = path.read_text(encoding="utf-8")
    header, _, rest = existing.partition("\n\n")
    path.write_text(f"{header}\n\n{body}{rest}", encoding="utf-8")


def test_capstone_scrape_pick_run_review(caplog):
    """The whole loop. Scrape Seek for KEYWORD. Pick the highest scored
    quick-apply job that has not yet been applied. Run apply_to_job in
    dry-run. Verify the cover-letter sidecar exists and the journal has
    questions answered (if Seek asked any).
    """
    from autoapply_next.engine import apply_to_job
    from autoapply_next.engine.results import ApplicationStatus
    from autoapply_next.engine.scraping import scrape_and_score

    caplog.set_level(logging.INFO)
    started = datetime.now()
    log: list[str] = []

    def log_status(message: str) -> None:
        log.append(f"[scrape] {message}")

    # Phase A: scrape and score for the keyword.
    scrape_result = asyncio.run(
        scrape_and_score(
            keyword=KEYWORD,
            engine_workdir=LIVE_ENGINE_WORKDIR,
            on_status=log_status,
            max_jobs=6,  # keep capstone fast; we only need ONE viable job
        )
    )

    journal: list[str] = [
        f"## Capstone {started.isoformat(timespec='seconds')}",
        f"- keyword: {KEYWORD}",
        f"- total_scraped: {scrape_result.total_scraped}",
        f"- new_jobs: {scrape_result.new_jobs}",
        f"- scored: {len(scrape_result.scored)}",
        f"- scrape_errors: {len(scrape_result.errors)}",
    ]
    for s in scrape_result.scored:
        journal.append(
            f"  - score={s.score} {s.title!r} {s.company!r} {s.url}"
        )

    # Pick the highest-scored job that is quick-apply (we already filtered to
    # quick-apply via the scraper). If everything scored 0 or None, pick the
    # first one with a description anyway; we still want to exercise apply.
    candidates = [
        s for s in scrape_result.scored
        if s.score is not None and s.score > 0
    ]
    if not candidates and scrape_result.scored:
        candidates = scrape_result.scored
    if not candidates:
        _append_test_log(journal + ["- outcome: NO_JOBS_TO_TEST"])
        pytest.skip("Scrape returned no scoreable jobs. Try a different keyword.")

    candidates.sort(key=lambda s: -(s.score or 0))
    # The scraper optimistically marks every Seek listing as easy_apply=True
    # but the real peek can reveal it is an external-ATS job. Walk
    # candidates in score order and stop at the first one that yields a
    # status we can assert on (DRY_RUN_VERIFIED or SKIPPED_LOW_SCORE).
    progress_lines: list[str] = []

    def on_progress(event):
        progress_lines.append(f"[{event.stage.value}] {event.message}")

    pick = None
    result = None
    attempts: list[str] = []
    for cand in candidates[:5]:  # max 5 retries; bounded against thrash
        progress_lines = []
        attempts.append(f"trying score={cand.score} {cand.url}")
        result = asyncio.run(
            apply_to_job(
                job_url=cand.url,
                engine_workdir=LIVE_ENGINE_WORKDIR,
                on_progress=on_progress,
                allow_real_submit=False,
                match_threshold=0,
            )
        )
        if (
            result.exception_type == "JobNotQuickApplyError"
            or (
                result.status.value == "failed"
                and "not a Seek quick-apply listing" in (result.error_message or "")
            )
        ):
            attempts.append(
                f"  -> not quick-apply, retrying with next candidate"
            )
            continue
        pick = cand
        break
    journal.append(f"- attempts: {len(attempts)}")
    for line in attempts:
        journal.append(f"  - {line}")
    if pick is None:
        _append_test_log(journal + ["- outcome: ALL_CANDIDATES_NON_QUICK_APPLY"])
        pytest.skip(
            "All top-scored candidates turned out to be external-ATS. "
            "Re-run later with a fresher scrape."
        )
    journal.append(f"- picked: score={pick.score} {pick.url}")

    elapsed = (datetime.now() - started).total_seconds()
    journal.append(f"- run_status: {result.status.value}")
    journal.append(f"- run_score: {result.score}")
    journal.append(f"- run_elapsed_total_sec: {elapsed:.1f}")
    journal.append(f"- resume_pdf: {result.resume_pdf}")
    journal.append(f"- cover_pdf: {result.cover_pdf}")
    journal.append(f"- screenshot: {result.dry_run_screenshot}")
    journal.append(f"- screening_answers: {len(result.screening_answers or [])}")
    for ln in progress_lines:
        journal.append(f"  - {ln}")

    # Phase C: verify cover letter sidecar (the bit Results renders).
    cover_letter_text_found = result.cover_letter_text is not None
    sidecar_exists = False
    if result.cover_pdf is not None:
        candidates_paths = [
            Path(str(result.cover_pdf) + ".txt"),
            LIVE_ENGINE_WORKDIR / Path(str(result.cover_pdf) + ".txt"),
        ]
        for cand in candidates_paths:
            if cand.exists():
                sidecar_exists = True
                break
    journal.append(f"- cover_letter_text_in_result: {cover_letter_text_found}")
    journal.append(f"- cover_letter_sidecar_exists: {sidecar_exists}")

    # Phase D: verify journal entry on disk for the URL.
    j_path = LIVE_ENGINE_WORKDIR / "errors" / "applications.jsonl"
    journal_url_match = False
    if j_path.exists():
        try:
            for line in reversed(j_path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("url") == pick.url:
                    journal_url_match = True
                    journal.append(
                        f"- journal_questions_recorded: {len(entry.get('questions_answered') or [])}"
                    )
                    break
        except Exception as exc:
            journal.append(f"- journal_read_error: {exc}")
    journal.append(f"- journal_entry_found: {journal_url_match}")

    _append_test_log(journal)

    # Headline assertions for the capstone:
    if result.status == ApplicationStatus.FAILED:
        raise AssertionError(
            f"Capstone failed: {result.exception_type}: {result.error_message}\n"
            f"Progress: {progress_lines}"
        )
    assert result.status in (
        ApplicationStatus.DRY_RUN_VERIFIED,
        ApplicationStatus.SKIPPED_LOW_SCORE,
    ), f"Unexpected status: {result.status}"

    if result.status == ApplicationStatus.DRY_RUN_VERIFIED:
        # Cover letter must be reviewable. EITHER the result has the text
        # (the adapter populates it from EngineHooks) OR the sidecar exists
        # on disk (the adapter persists it).
        assert cover_letter_text_found or sidecar_exists, (
            "Cover letter not visible to Results screen: "
            f"text_in_result={cover_letter_text_found}, sidecar={sidecar_exists}"
        )
        assert result.dry_run_screenshot and Path(result.dry_run_screenshot).exists(), (
            f"screenshot missing: {result.dry_run_screenshot}"
        )
