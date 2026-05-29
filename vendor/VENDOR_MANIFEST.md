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
