# ADR-0009: Surgical re-vendor of seek_apply.py for candidate screening facts

Date: 2026-06-12
Status: Accepted

## Context

The vendored engine (`vendor/job-finder/`, pinned 2026-05-29) predates a
critical capability the live engine at
`/Users/sagarverma/Pictures/Claude-experiments/job-finder` gained: a
candidate-facts layer in `seek_apply.py` that answers Seek's screening
questions deterministically. The live `seek_apply.py` is 2384 lines; the
29-May vendored copy is 2155. The 229-line delta is, almost entirely:

- `_load_candidate_facts()` plus a block of `CAND_*` module globals read
  from `config.yaml`'s `candidate:` block at import time (citizenship,
  work rights, security clearance, visa label/expiry, salary target,
  notice period, work arrangement, drivers licence, relocation, and the
  diversity questions).
- A more developed citizenship / work-rights question handler.
- `_claude_salary_estimate()` and a target-aware `_pick_salary_option`
  for Seek salary-band dropdowns.

Without this layer the desktop app, which drives the vendored engine,
cannot answer eligibility questions deterministically. It would fall back
to model-guess or default strategies and is far more likely to mis-answer
citizenship/work-rights/clearance or stall on validation, which on a live
autonomous run means wrong answers or silent failures filed under the
user's name. This was the highest-severity divergence found in the
2026-06-12 reconnaissance.

ADR-0001 forbids in-place edits of `vendor/` but explicitly allows a
re-vendor (fork-and-pin update) recorded as its own ADR.

## Decision

Re-vendor `seek_apply.py` ONLY, copied verbatim from the live engine.
This is a surgical re-pin of a single source file, not a hand edit and
not a wholesale tree replacement.

Scope was kept to `seek_apply.py` deliberately:

- It is the only file carrying the screening-answer logic the app needs.
- `matcher.py`, `applicator.py`, `models.py`, `process_lock.py`,
  `utils.py`, `claude_cli.py`, `tailorer.py` are byte-identical between
  the live and vendored trees, so they need no update.
- `main.py` differs (+174 lines) but the app does not import it; the app
  has its own worker orchestration (ADR-0008). Re-vendoring it would add
  unused code and review surface for no behavior change.
- `tracker.py` (+12 lines) and `scraper/seek.py` (per-skill scraping)
  differ but their deltas are unrelated to the deferred items, and the
  app's current behavior against them is covered by the green suite.
  Updating them is scope creep with its own regression risk, so they are
  intentionally left at the 2026-05-29 pin.

## Why this is low risk

The two seams the app monkey-patches are unchanged. Verified before the
copy:

- `seek_apply._submit(page)` is byte-identical live vs vendored (the
  SafetyGate patch target).
- `seek_apply._verify_applied(page, job_title, job_company)` is
  byte-identical (the RobustVerifier patch target).
- `_tick_terms_checkbox` and `_scrape_applied_cards` are byte-identical.
- Every function/class the adapter calls (`peek_is_quick_apply`,
  `fetch_seek_jd`, `apply_seek_quick`, `_PeekSession`, `_Journal`,
  `SeekApplyError`, `ExternalApplyError`) is present with the same
  signature.
- The submit-button selector list the SafetyGate mirrors
  (`["Submit application", "Submit", "Apply now", "Apply"]`) is identical
  to the engine's, so the drift contract test still holds.
- `_load_candidate_facts()` is fail-soft (try/except returning `{}`) and
  every `CAND_*` global has a default, so importing `seek_apply` with a
  config that lacks a `candidate:` block degrades to defaults rather than
  raising. The 214 non-live tests, which import the module without a full
  candidate config, stay green (confirmed before and after the copy).

`JobNotQuickApplyError` is defined in the app's own `adapter.py`, not the
engine, so it is unaffected.

## Consequences

Correct, deterministic screening answers now require the runtime engine
workdir's `config.yaml` to carry the full `candidate:` block. The live
config already has it; the production app must seed its workdir from a
config that includes it (see the workdir-seeding work in the
user-ready sprint). A workdir config without the block silently falls
back to the hardcoded defaults in `seek_apply.py` (Sydney / 485 visa /
110k target), which would be wrong for a different user, so workdir
seeding must copy a complete candidate block, not a stub.

If the engine is re-vendored wholesale later, this surgical pin should be
folded into that and this ADR superseded.
