# Vendor Manifest

This directory contains pinned copies of third-party / sibling-project code that the AutoApply Next app depends on. **Do not edit anything under `vendor/`.** If an upstream fix is needed, re-vendor at a new pin and record the change here.

## job-finder

- **Source:** `/Users/sagarverma/Pictures/Claude-experiments/job-finder`
- **Upstream git HEAD at vendor time:** `c85c68d2c7cf80f63727a21e36e49e32dba7c950` (2026-04-11)
- **Working-tree state at vendor time:** dirty. The following files differed from HEAD when copied:
  `applicator.py`, `assets/SAGAR VERMA.docx`, `assets/profile.txt`, `check_seek_login.py`, `delete_all_resumes.py` (and other modified files visible in `git status` at vendor time).
  This is intentional: the live engine that produced our test signal is in a dirty state, and that is what the GUI must match. Re-vendoring at a clean commit must be done deliberately and is its own ADR.
- **Vendor time:** 2026-05-29
- **Removed at vendor time:** `.git/`, `__pycache__/`, `sessions/`, `output/`, `jobs.db`, `bot.log*`, `.worktrees/`, `.superpowers/`. Runtime data does not vendor.
- **License / authorship:** in-house; same author as the GUI.
- **Contract the GUI depends on:** see `docs/adr/0001-inventory-and-minimal-product.md`.

## Re-vendor procedure

1. Verify upstream is in the state you intend (commit + working-tree changes).
2. `rm -rf vendor/job-finder` then `cp -R <upstream> vendor/job-finder`.
3. Strip the runtime dirs and `.git` as listed above.
4. Update this file with the new HEAD and date.
5. Run the contract test suite (`pytest tests/contract`). All must pass before merging.
6. Note any adapter changes required as a new ADR.

## Re-vendor history

### 2026-06-12: surgical re-pin of `seek_apply.py`

`seek_apply.py` only was re-copied verbatim from the live source
(`/Users/sagarverma/Pictures/Claude-experiments/job-finder/seek_apply.py`,
2384 lines) to bring the candidate screening-facts layer
(`_load_candidate_facts` + `CAND_*` globals, the developed
citizenship/work-rights handler, `_claude_salary_estimate` and the
target-aware `_pick_salary_option`). All other vendored files stay at the
2026-05-29 pin. This is a deliberate, ADR-recorded re-vendor of a single
file, not an in-place edit. Rationale, the scope decision, and the seam
verification are in `docs/adr/0009-revendor-seek-apply-candidate-facts.md`.

Verified: the two patched seams (`_submit`, `_verify_applied`) plus
`_tick_terms_checkbox` and `_scrape_applied_cards` are byte-identical to
the prior pin; the SafetyGate selector list is unchanged; all 214
non-live tests pass before and after the copy.
