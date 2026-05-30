# AutoApply Next: iterate-fix test log

## Capstone 2026-05-30T11:40:12
- keyword: site reliability engineer
- total_scraped: 531
- new_jobs: 525
- scored: 6
- scrape_errors: 0
  - score=2 'Senior Civil Design Engineer' 'SSA Group' https://au.seek.com/job/92420998
  - score=2 'Commercial Construction - Site Supervisory Staff - All Levels' 'ECi EXECUTiVE' https://au.seek.com/job/92420551
  - score=2 'Mining Engineer - Ventilation' 'Evolution Mining' https://au.seek.com/job/92420276
  - score=2 'Mining Engineer Planning' 'Evolution Mining' https://au.seek.com/job/92420216
  - score=2 'Aircraft Maintenance Engineer (Structures)' 'BAE Systems' https://au.seek.com/job/92420211
  - score=2 'Principal Fire Protection Engineer' 'AECOM Australia PTY LTD' https://au.seek.com/job/92419726
- attempts: 1
  - trying score=2 https://au.seek.com/job/92420998
- picked: score=2 https://au.seek.com/job/92420998
- run_status: dry_run_verified
- run_score: 2
- run_elapsed_total_sec: 488.5
- resume_pdf: output/SagarVerma__SeekListing92420998_20260530_114635_377.pdf
- cover_pdf: output/CoverLetter__SeekListing92420998_20260530_114638_212.pdf
- screenshot: /Users/sagarverma/Pictures/Claude-experiments/job-finder/output/dryrun-screenshots/dryrun-submit-ready-20260530T114819.png
- screening_answers: 0
  - [peek] Fetching listing https://au.seek.com/job/92420998
  - [score] Scoring match
  - [score] Score 2/100
  - [tailor] Generating tailored resume + cover letter
  - [tailor] Tailored documents ready
  - [apply] Driving Seek apply form (dry-run)
  - [dry_run_verified] Dry-run reached submit-ready (button='Submit application')
- cover_letter_text_in_result: True
- cover_letter_sidecar_exists: True
- journal_questions_recorded: 0
- journal_entry_found: True

## Capstone 2026-05-30T11:35:29
- keyword: site reliability engineer
- total_scraped: 531
- new_jobs: 531
- scored: 6
- scrape_errors: 0
  - score=2 'Mobile Plant Maintenance Supervisor' 'REGROUP Australia' https://au.seek.com/job/92421802
  - score=4 'Project Engineer - WA Projects | Perth Based | Site role' 'Liberty Industrial' https://au.seek.com/job/92421763
  - score=3 'Precast Project & Design Manager for Industry Leader' 'Talent X Pty Ltd' https://au.seek.com/job/92421282
  - score=2 'Mining Engineer' 'Harmony Australasia' https://au.seek.com/job/92421241
  - score=2 'Structural Engineer for Patented One-Of-A-Kind Product!' 'Talent X Pty Ltd' https://au.seek.com/job/92421162
  - score=2 'Structural Engineer for Patented One-Of-A-Kind Product!' 'Talent X Pty Ltd' https://au.seek.com/job/92421163
- picked: score=4 https://au.seek.com/job/92421763
- run_status: failed
- run_score: None
- run_elapsed_total_sec: 243.5
- resume_pdf: None
- cover_pdf: None
- screenshot: None
- screening_answers: 0
  - [peek] Fetching listing https://au.seek.com/job/92421763
  - [failed] peek failed: JobNotQuickApplyError: Job is not a Seek quick-apply listing: https://au.seek.com/job/92421763
- cover_letter_text_in_result: False
- cover_letter_sidecar_exists: False
- journal_entry_found: False

## Capstone 2026-05-30T11:32:14
- keyword: site reliability engineer
- total_scraped: 531
- new_jobs: 0
- scored: 0
- scrape_errors: 0
- outcome: NO_JOBS_TO_TEST

## Capstone 2026-05-30T11:29:28
- keyword: AWS
- total_scraped: 212
- new_jobs: 0
- scored: 0
- scrape_errors: 0
- outcome: NO_JOBS_TO_TEST

## What this is

This file is the running log of the live-Seek dry-run integration test, the
slowest and most consequential test in the suite. Each run drives a real
Seek listing all the way to submit-ready and stops at the safety gate from
ADR-0004. Real applications are never filed by this test; the whole point
of the gate is that `ALLOW_REAL_SUBMIT` is `False` and the dry-run path
captures a screenshot and raises `DryRunReached` instead of clicking the
submit button.

## How to run it

```
pytest tests/integration -m requires_live_seek
```

The `requires_live_seek` marker is declared in `pyproject.toml` and is
opt-in. CI does not select it. To run the loop locally you need:

- A populated `engine_workdir` per the README, including
  `sessions/seek_chrome_profile/` with a current Seek login.
- LibreOffice on PATH for the docx-to-pdf step.
- The Claude CLI logged in (Max subscription) for the score and tailor
  stages.

The test fails fast if any of those is missing.

## The iterate-fix loop

When the test fails, the fix loop is: read the failure, find the smallest
real engine-side cause, change the test fixture or the candidate URL
selection, rerun. The anti-thrash budget is **3 iterations per failure**.
If the same failure mode survives three rounds of fixes, stop the loop and
write a separate note in `docs/adr/` before touching anything else. The
budget exists because the test is slow (3-5 minutes per run) and because
"one more tweak" against a real third-party site is exactly the shape of
work that goes nowhere.

## Picking a job URL

Runs hit real Seek listings via the persistent Chrome session, so the URLs
the test points at must be both **quick-apply** (an "Easy Apply" listing,
not an external-redirect listing) and **unapplied** (not in the engine's
`applications` table; Seek's submit button reads "Already applied" if you
have, and the engine refuses). A fresh URL is found by:

1. Open Seek in the persistent Chrome session.
2. Search for listings with the green Quick Apply badge.
3. Pick one the candidate has not applied to in the engine's `jobs.db`.
4. Confirm by calling `seek_apply.peek_is_quick_apply(url, session_state)`
   directly; it returns `(True, page)` for valid candidates.

The test fixture in `tests/integration/test_live_seek_dryrun.py` probes
candidates from the `seen_jobs` table minus the `applications` table and
filters to verified quick-apply via the same peek call.

## Fix cycle, 2026-05-29

We went through one fix cycle to get to a passing run. Both attempts are
preserved in the auto-appended entries below.

**Iteration 1, failed.** URL `https://au.seek.com/job/92399024` was not a
quick-apply listing. The adapter raised `JobNotQuickApplyError`, which is
the correct behaviour: the minimal product (ADR-0001) refuses non-quick-
apply listings, and the engine's `peek_is_quick_apply` returned `False`.
Elapsed 11.2 seconds. No score, no tailor, no apply stages reached. The
fix was not in the code; it was in the URL-selection logic of the test.
We changed the fixture to probe candidate URLs from `seen_jobs` minus
`applications` and pick verified quick-apply ones via
`seek_apply.peek_is_quick_apply` before handing them to the adapter.

**Iteration 2, passed.** URL `https://au.seek.com/job/92398511` ran
end-to-end in 238.2 seconds. The matcher scored the job 4 out of 100;
the test fixture sets `match_threshold=0` to force the path through every
stage rather than stopping at `SKIPPED_LOW_SCORE`, which is the right
posture for an integration test of the apply path. All five stages
reached: peek, score, tailor, apply, dry-run-verified. The gate located
the submit button on the first selector (`'Submit application'`), ticked
the terms checkbox, asserted the button was visible and enabled,
captured a full-page screenshot at
`/Users/sagarverma/Pictures/Claude-experiments/job-finder/output/dryrun-screenshots/dryrun-submit-ready-20260529T144505.png`,
and raised `DryRunReached`. The adapter caught it and returned
`ApplicationStatus.DRY_RUN_VERIFIED`. **No real application was
submitted.**

The engine wrote two PDFs to disk:

- `output/SagarVerma__SeekListing92398511_20260529_144314_505.pdf`: the
  tailored resume.
- `output/CoverLetter__SeekListing92398511_20260529_144317_702.pdf`: the
  tailored cover letter.

These are the artefacts the Results screen
(`src/autoapply_next/ui/results_screen.py`) will surface to the user for
review before any decision to toggle `ALLOW_REAL_SUBMIT`. The reviewer
opens the PDFs, reads the screening answers captured by `EngineHooks`
from the engine's journal, looks at the dry-run screenshot, and only
then has enough evidence to flip the flag. That review-then-flip pattern
is the safety story end-to-end; the test verifies the dry-run half of
it, the Results screen presents the evidence, and the user owns the
decision.

## Auto-appended entries

The integration test appends a YAML-like entry per run below this line.
Most recent at the top.

## 2026-05-29T14:41:08
- job_url: https://au.seek.com/job/92398511
- status: dry_run_verified
- elapsed_sec: 238.2
- score: 4
- exception: None
- error: None
- resume_pdf: output/SagarVerma__SeekListing92398511_20260529_144314_505.pdf
- cover_pdf: output/CoverLetter__SeekListing92398511_20260529_144317_702.pdf
- screenshot: /Users/sagarverma/Pictures/Claude-experiments/job-finder/output/dryrun-screenshots/dryrun-submit-ready-20260529T144505.png
- progress: 7 events
  - [peek] Fetching listing https://au.seek.com/job/92398511
  - [score] Scoring match
  - [score] Score 4/100
  - [tailor] Generating tailored resume + cover letter
  - [tailor] Tailored documents ready
  - [apply] Driving Seek apply form (dry-run)
  - [dry_run_verified] Dry-run reached submit-ready (button='Submit application')

## 2026-05-29T14:40:09
- job_url: https://au.seek.com/job/92399024
- status: failed
- elapsed_sec: 11.2
- score: None
- exception: JobNotQuickApplyError
- error: Job is not a Seek quick-apply listing: https://au.seek.com/job/92399024
- resume_pdf: None
- cover_pdf: None
- screenshot: None
- progress: 2 events
  - [peek] Fetching listing https://au.seek.com/job/92399024
  - [failed] peek failed: JobNotQuickApplyError: Job is not a Seek quick-apply listing: https://au.seek.com/job/92399024

Each entry below records one run of the live-Seek dry-run integration test. The most recent entry is at the top.

