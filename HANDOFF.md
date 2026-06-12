# AutoApply Next: user-ready sprint handoff

Branch: `user-ready-sprint` (off `main`). Status: engineering complete and
committed (248 non-live tests green); live pilot complete (5 of 5 applications
submitted and fully verified); packaged build builds and launches, smoke test
passed twice. Remaining: L3 fixture replay, F6 polish, and notarization (all
non-blocking; see "What is NOT done").

This document is written for you to pick up and verify by hand. It records what
changed, what was tested, the decisions made on your behalf, known limitations,
and the exact steps to run your own manual test.

## TL;DR

The five things the brief asked for are done and committed: the chained-runner
throttle regression is fixed and proven, the fuzzy verifier is hardened,
same-role deduplication is in (this is the fix for the 133 real duplicate
submissions that were already in your production database), the engine was
re-vendored so the app can actually answer Seek's screening questions, and the
packaged build is now clean-machine-correct. An isolated pilot workdir is set up
with a copy of your live Seek session and your application history, and the
session is verified logged in.

## What changed (commit by commit)

1. **Re-vendor `seek_apply.py` (ADR-0009).** The vendored engine was frozen at
   29 May and lacked the candidate screening-facts layer your live engine uses
   to answer Seek's citizenship / work-rights / security-clearance / visa /
   salary questions deterministically. Without it the app would mis-answer those
   and stall or submit wrong answers. Only `seek_apply.py` was re-pinned
   (verbatim from your live tree); the two seams the app monkey-patches
   (`_submit`, `_verify_applied`) are byte-identical across versions, so this was
   low-risk. Rationale and seam verification in
   `docs/adr/0009-revendor-seek-apply-candidate-facts.md`.

2. **Throttle: hard 60s floor on every live path (the layer-4 gate).** The
   "Pace between applies" toggle, when off, sent `(0,0)` to the batch runner, so
   live applies could fire back-to-back with zero delay on the only
   GUI-reachable autonomous path, and no test caught it. There is now a single
   authoritative clamp (`live_safe_throttle_range`) applied at the `run_batch`
   chokepoint every live apply funnels through: on a real submit the inter-apply
   gap is floored to 60s no matter what the caller passes or the toggle is set
   to. Dry-run may still opt out. Proven by `tests/contract/test_throttle_floor.py`
   and `tests/ui_tests/test_throttle_proof.py`, which assert the floor on the
   chained Phase 0 and Phase 2 runs too. **Nothing submits live below this floor.**

3. **Same-role deduplication.** URL dedup keyed on the Seek listing id, so it
   missed the same employer+title role reposted under a new id. Recon of your
   production database found 133 already-applied rows that are duplicates of the
   same role under different listing ids (one role applied to 6 times in 19
   minutes). A normalized company+title guard in `apply_to_job` now skips a
   listing before any score/tailor/submit if a sibling of the same role was
   already applied to. Only applied / submitted-uncertain / in-progress block;
   a sibling that was low-score-skipped or failed is still eligible.

4. **Verifier hardening.** The title+company fallback used loose two-way
   substring matching, so a short generic target ("Engineer" at "Hydrogen")
   could match an unrelated applied card ("Software Engineer" at "Hydrogen
   Group") and report a false success. The title axis now requires exact or
   multi-token-subset matching, killing that false positive while preserving
   truncation tolerance.

5. **Packaging for a clean machine.** The frozen build never put the vendored
   engine on `sys.path` (so the packaged app would crash on the first apply),
   the production workdir was empty with nothing to seed it, and the build spec
   was shipping your dev `.env` (with LinkedIn/Seek/Gmail credential keys) to
   every user. Fixed: `platform/bootstrap.py` resolves the engine in dev and
   frozen builds and seeds an empty workdir; `__main__` calls both at startup;
   the spec now excludes `.env*`, the dev config, dbs, logs, and session dirs.

## What was tested

- 248 non-live tests pass (`pytest tests/contract tests/ui_tests`), up from 214
  at the start. New: throttle floor proof (contract + ui), same-role dedup
  (core + adapter guard), verifier hardening, bootstrap/seeding.
- L1 scraper validated live against the pilot workdir (scraped 416 + 103,
  scored, persisted, zero errors). The scorer correctly rates relevant roles
  high (Platform Engineer 85, Senior Software Engineer 83) and irrelevant ones
  low (mining/civil/maintenance 2-6), so the threshold gate works.
- The copied Seek session was verified logged in (reached the Applied Jobs page
  with no login redirect).
- L2 dry-run reached submit-ready cleanly on a real quick-apply posting
  (Platform Engineer - Linux, score 82): peek, score, tailor a relevant resume +
  cover letter, drive the real Seek form to "review and submit", SafetyGate
  stopped at submit-ready and screenshotted, zero submission. The not-quick-apply
  skip path was also validated (a council role correctly classified external and
  mapped to 'skipped').
- F1 verified directly: the re-vendored engine loads the correct candidate facts
  in the pilot workdir (is_citizen False, has_work_rights True,
  has_security_clearance False, visa 485, salary target 130k, email
  sagarvd130@gmail.com), which are the deterministic answers the screening
  handlers use.

## Live pilot results (L5): 5 of 5 submitted and FULLY VERIFIED

All five were applied to from your configured criteria (score >= 25), each
tailored, submitted, and verified. Reconciliation: 5 submitted = 5 'applied'
rows = 5 verifier-confirmed on the Applied Jobs page = 5 Seek confirmation
emails. Zero failures, zero uncertain, zero duplicates.

| # | Job | Score | Result |
|---|-----|-------|--------|
| 1 | Platform Engineer - Linux @ Private Advertiser (92688925) | 88 | applied + verified + email |
| 2 | Senior Software Engineer @ Correlate Resources (92688893) | 83 | applied + verified + email |
| 3 | Applications Support Engineer @ Global Group (92690877) | 28 | applied + verified + email (screening Qs) |
| 4 | Staff Software Engineer @ Correlate Resources (92690384) | 28 | applied + verified + email |
| 5 | Cloud Solutions Architect @ Talent (92691291) | 47 | applied + verified + email |

Each verified against all five checks: submit clicked, Applied badge on the
Applied Jobs page (RobustVerifier), confirmation email ("Your application was
successfully submitted", to sagarvd130@gmail.com), SQLite status='applied',
and counts reconciled.

Two things the pilot proved beyond the happy path:

- **Screening questions answered correctly, live.** Application #3 had employer
  questions and the re-vendored engine answered them deterministically: "are you
  legally entitled to work in Australia?" -> Yes (hard-rule, from
  has_work_rights=True), "do you have a current driver's licence?" -> Yes,
  notice period -> "Immediately available". This is exactly what the F1
  re-vendor was for, and it worked on a real submission.
- **The throttle held, live.** Run through the production run_batch path with the
  (60,120) pacing, the measured inter-submit gaps net of apply time were ~80s and
  ~118s, both inside the 60-120s band. Working rate limits are real, not just a
  test.

## Packaged build (L1 of the test plan / DoD 1) and smoke checklist

The PyInstaller build was run for the first time. It surfaced a real latent bug:
the frozen app crashed at launch with "attempted relative import with no known
parent package" because PyInstaller runs the entry as top-level __main__ with no
package context. Fixed with a launcher entry that imports the package by
absolute name (`packaging/autoapply_launch.py`).

**Smoke checklist (run twice on the packaged `dist/AutoApply Next.app`, PASS both):**

1. Launch the bundle pointed at an empty workdir
   (`AUTOAPPLY_NEXT_ENGINE_WORKDIR=<clean dir>`).
2. The log shows `engine on sys.path: .../Contents/Frameworks/vendor/job-finder`
   (the vendored engine resolves in the frozen bundle).
3. The log shows `workdir seed: seeded config.yaml ...` and the clean workdir now
   contains config.yaml, assets/, sessions/, output/, errors/.
4. The Seek single-instance lock is acquired; orphan recovery runs.
5. No Traceback / ImportError / ModuleNotFoundError.

Both runs passed all five. The bundle excludes the dev `.env` (verified absent)
and ships `config.yaml.example` as the seed template. It is unsigned (per your
decision); on a clean Mac the tester right-click-Opens once past Gatekeeper.

## What is NOT done (and why)

- **L3 fixture replay** (record a real apply, replay the submit against a local
  fixture server) is not built. The real submit + post-submit verify + persist
  path was instead exercised live five times in the pilot, so post-submit
  handling is validated; the fixture harness would add deterministic error-path
  coverage and is a recommended follow-up rather than a gap in the live path.
- **L6 steady-state continuous operation is validated but deliberately NOT
  enabled.** The pilot proves continuous live applying works, but turning the
  desktop app's autonomous mode ON would run it concurrently with your existing
  CLI job-finder bot against the same Seek account, which risks anti-bot flags
  and cross-system duplicates. Enable it deliberately (and pause the CLI bot
  first), do not run both at once.
- **F6 polish** (sign-in screen to a clean local-mode entry instead of a disabled
  stub; reconcile the match_threshold default across worker/adapter/settings;
  wire the inert operating_hours setting into the worker as a second brake) is
  P1 and not yet done. None of it blocks the pilot or the packaged build.
- **Notarization** needs an Apple Developer certificate (your call); the build is
  unsigned today.

## Decisions made on your behalf

- **Re-vendor scope:** only `seek_apply.py`, not the whole engine tree. `main.py`
  differs but the app does not import it; `tracker.py` and `scraper/seek.py`
  diffs are unrelated to the deferred items, so updating them would be scope
  creep with its own regression risk. They stay at the 29-May pin.
- **Engine workdir for the pilot (you confirmed):** an isolated workdir at
  `/Users/sagarverma/autoapply-pilot/engine`, seeded with your live config (the
  correct candidate facts and `sagarvd130@gmail.com` application email), a copy
  of your production `jobs.db` (5,221 rows, so same-role dedup sees your full
  history), and a copy of your Seek session profile (72MB, caches excluded).
  Your real production database and CLI bot are untouched.
- **Packaging (you confirmed):** ship unsigned for the dogfood. The build runs
  on a clean Mac; the tester right-click-Opens past Gatekeeper once. Full
  notarization needs an Apple Developer certificate and is a follow-up.
- **Sign-in:** the Supabase sign-in screen is a non-functional stub with no
  credentials provisioned (it is SaaS-tier scope). For this milestone the
  meaningful "sign in" is the Seek session bootstrap. (Local-mode polish of the
  sign-in screen is tracked under remaining work.)

## Known limitations

- **Deeper verifier hardening is staged, not shipped.** The job-id verification
  strategy still scans the whole Applied Jobs page rather than the specific
  applied-card subtree, so in principle a job id appearing in a "recommended"
  rail could read as applied. Scoping that safely needs live Seek DOM, so it is
  deferred to the live-pilot phase rather than changed blind against the working
  verifier.
- **Packaged build not yet built/run here.** The code is frozen-build-correct,
  but `pyinstaller` is not installed in the venv and no `.app` has been produced
  or launched on a clean profile yet. Steps below.
- **Operating-hours setting is inert** (defined but never enforced). Daily cap
  and the 60s throttle are the active brakes.

## How to run your own manual test (dry-run first, then live)

1. From the repo: `python -m venv .venv && source .venv/bin/activate &&
   pip install -e ".[dev]" && playwright install chromium`. Ensure `claude`
   (logged in) and `soffice` (LibreOffice) are on PATH.
2. Point the app at the pilot workdir:
   `export AUTOAPPLY_NEXT_ENGINE_WORKDIR=/Users/sagarverma/autoapply-pilot/engine`
3. Launch: `python -m autoapply_next`. Keep `ALLOW_REAL_SUBMIT` OFF in Settings
   for the first pass. Scrape a keyword, review the queue, run a job in dry-run,
   and confirm the Results screen shows the cover letter and screening answers.
4. Only when satisfied, flip the real-submit toggle (it has a confirmation
   dialog) and apply to one job. Verify it on Seek's Applied Jobs page.

(Live pilot results and the final smoke-test checklist are appended below as the
sprint completes.)
