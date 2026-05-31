# Parallel resilience fix: file partition + shared contracts

This document is the contract every workstream codes against. **No
workstream may write a file that is owned by another.** Read-only across
boundaries is fine.

The orchestrator has audited the repo and confirmed every file referenced
below exists at HEAD `362e317`. Engine source under `vendor/job-finder/`
is byte-for-byte off limits; importing from it (e.g. `process_lock.seek_lock`)
is allowed.

## File partition (exclusive write)

| WS | Files owned (exclusive write) | Read-only imports it depends on |
|---|---|---|
| **A** lifecycle | `src/autoapply_next/__main__.py`, NEW `src/autoapply_next/startup.py`, NEW `tests/contract/test_startup.py` | `engine/persistence.py` (Contract 1 API), `platform/paths.py`, `safe_logging/scrubber.py`, `safe_ui/__init__.py` |
| **B** persistence + recovery | `src/autoapply_next/engine/persistence.py`, `src/autoapply_next/engine/scraping.py`, NEW `tests/contract/test_recovery.py`, MAY UPDATE existing `tests/contract/test_persistence.py` | `engine/results.py`, `engine/verifier.py` |
| **C** adapter retry + persist check | `src/autoapply_next/engine/adapter.py`, NEW `tests/contract/test_adapter_retry.py` | `engine/persistence.py` (Contracts 1/2/4 API), `engine/verifier.py`, `engine/safety.py`, `engine/hooks.py` |
| **D** batch pacing + circuit breaker | `src/autoapply_next/engine/batch.py`, NEW `tests/contract/test_batch_circuit.py` | `engine/adapter.py` (apply_to_job), `engine/persistence.py` (Contract 2 is_fatal_condition + Contract 5 BatchRunResult shape) |
| **E** worker tally + dialog coalescing | `src/autoapply_next/engine/worker.py`, `src/autoapply_next/safe_ui/error_handler.py`, NEW `tests/ui_tests/test_worker_tally.py`, MAY UPDATE existing `tests/ui_tests/test_worker_signals.py` for cancel-tally | `engine/batch.py` (Contract 5), `engine/adapter.py` |

UI screens (`src/autoapply_next/ui/*.py`) are deliberately **not** in any
workstream. None of them needs to change for the fixes in this pass.
Settings store keeps `daily_cap` (D consumes it).

Engine source under `vendor/job-finder/` is **not owned by any workstream
and must not be edited**. The vendored `process_lock.seek_lock()` may be
imported by A (read-only).

## Shared contracts

These are the interfaces. Workstream B implements all of contracts 1–4
(except the parts that live in A's startup wiring). Other workstreams
write to these signatures verbatim.

### Contract 1 — `in_progress` status + recovery

Add `"in_progress"` as a valid `applications.status` value. It is *not*
in `TERMINAL_STATUSES`. It must be excluded from batch eligibility
(eligibility stays "status='queued' AND failure_count < PERMAFAIL_THRESHOLD"; see Contract 3).

```python
# persistence.py public surface

def persist_in_progress(
    *, engine_workdir: Path, url: str,
    title: str = "", company: str = "", score: int | None = None,
) -> "PersistResult":
    """Insert/update the row to status='in_progress' BEFORE applicator.apply().
    Preserves existing title/company if present, fills in only when row is new.
    Returns PersistResult (Contract 4)."""

@dataclass
class ReconcileResult:
    url: str
    prior_status: str
    new_status: str
    action: str  # "force_failed" | "verified_applied" | "verified_uncertain" | "verified_not_applied" | "no_change"
    note: str = ""

def recover_orphans(
    *, engine_workdir: Path,
    verifier_factory: "callable[[], RobustVerifier] | None" = None,
) -> list[ReconcileResult]:
    """Find every row in 'in_progress' (orphans from a crashed apply).
    If verifier_factory is None (default): flip them to 'failed' with a note.
    If verifier_factory is provided: would use the verifier to reconcile against
    Seek (out of scope for this pass; default path is enough to ship)."""
```

A calls `recover_orphans(engine_workdir=...)` (no verifier) on startup
before the UI is interactive. The watchdog QTimer (A) also calls
`recover_orphans` but treats rows older than 15 min only — that filter is
A's responsibility (just `time.time() - row.timestamp > 15*60`).

### Contract 2 — `map_status` additions + `is_fatal_condition`

`persistence.map_status` adds these mappings (extension; existing mappings
unchanged):

- `FAILED + exception_type == "CoverLetterQualityError"` → `"skipped"`
  (was `"failed"`; matches job-finder).
- `FAILED + exception_type == "PermissionError"` → `"skipped"`
  (session not loaded; re-queueing wouldn't fix it).
- `FAILED + exception_type == "BoardBlockedError" and "session expired" in error_message.lower()` → `"skipped"`.

```python
# persistence.py
def is_fatal_condition(
    *, exception_type: str | None, error_message: str = ""
) -> str | None:
    """Classify whether an apply failure is fatal-for-batch (i.e. should
    halt the batch and leave the rest 'queued'). Returns a short
    human-readable reason if fatal, else None.

    Cases that are FATAL (return reason):
      - PermissionError                    -> "Seek session not loaded; bootstrap session and retry"
      - BoardBlockedError + "session expired" in msg -> same
      - BoardBlockedError + "captcha" in msg       -> "Seek anti-bot challenge detected; pause and verify by hand"
      - BoardBlockedError + "rate limit"|"blocked" in msg -> "Seek temporarily blocking us; pause for an hour"

    Cases that are NOT fatal:
      - SeekApplyError (stuck step, validation), TimeoutError, network
        errors -- they apply to one job only; the batch continues.

    Stateless. The batch circuit breaker (Workstream D) counts consecutive
    failures separately."""
```

### Contract 3 — Permafail threshold

```python
# persistence.py
PERMAFAIL_THRESHOLD = 3
"""failure_count >= this value means the URL has structurally failed
enough times that we stop including it in eligibility, mirroring
job-finder's permanently_failed_urls()."""
```

- `queued_urls_for_batch` SQL: now also `AND COALESCE(failure_count, 0) < ?`.
- `requeue_job` refuses with `CannotRequeueError` when `failure_count >= PERMAFAIL_THRESHOLD`, regardless of status.
- `scraping._has_application_row` is unchanged (any row already deduplicates), but a helper `permafailed_urls(workdir) -> set[str]` is added for tests and possible future logging.

### Contract 4 — `PersistResult`

```python
# persistence.py
@dataclass(frozen=True)
class PersistResult:
    written: bool
    """True if the DB write succeeded (or was intentionally a no-op for
    DRY_RUN/CANCELLED). False on actual write failures."""
    status: str | None
    """The status that was written. None for intentional no-ops."""
    error: str | None = None
    """Error message if written=False due to a write failure. None otherwise."""
```

`persist_apply_outcome` returns `PersistResult` (was `str | None`).
`persist_in_progress` also returns `PersistResult`. **Both must log an
ERROR (not WARNING) on actual write failures** and the adapter must
check `.written`.

If `persist_apply_outcome` for a `SUBMITTED` result returns
`written=False`, the adapter must downgrade the in-memory status to
`SUBMITTED_UNCERTAIN` with an error note. The engine already submitted;
we don't know the DB state.

### Contract 5 — Progressive tally on the batch runner

`batch.run_batch` accepts an optional `tally` object and mutates it in
place; returns the same object so existing callers still work.

```python
# batch.py
async def run_batch(
    *,
    job_urls: list[str],
    engine_workdir: Path,
    allow_real_submit: bool,
    on_progress: RunProgress | None = None,
    is_cancelled: CancelCheck | None = None,
    is_stopped: StopCheck | None = None,
    # NEW: pacing 60-120s default, mirrors job-finder APPLY_GAP_MIN/MAX.
    throttle_range_seconds: tuple[int, int] = (60, 120),
    # NEW (Contract 5):
    tally: BatchRunResult | None = None,
    # NEW: circuit-breaker hook (Contract 2):
    fatal_classifier: "callable[..., str | None] | None" = None,
    max_consecutive_failures: int = 3,
    # NEW: cap so select-all on hundreds can't run for hours.
    daily_cap: int = 0,  # 0 means no cap
) -> BatchRunResult:
    """The worker must construct a BatchRunResult, hand it in, then read it
    even if the coroutine raises CancelledError. Per-job mutations happen
    in-place. stop_reason is set on every exit path."""
```

`fatal_classifier` is `persistence.is_fatal_condition`-shaped (kwarg-only:
`exception_type=`, `error_message=`). The default is None; D wires it to
`is_fatal_condition` when constructing the runner. If a per-job result
matches a fatal condition, D sets `tally.stop_reason` to `"fatal:<reason>"`,
emits one prominent log line, and returns the tally immediately. Any
URL we have not yet processed remains untouched in `jobs.db` (still
`queued`), so the user can resume after fixing the root cause. No row is
marked `failed` because of the circuit breaker.

`max_consecutive_failures = 3` (configurable): after N consecutive
per-job results in (FAILED, SUBMITTED_UNCERTAIN+verify=UNCERTAIN with
network root cause) the runner also returns with
`stop_reason="consecutive_failures"`.

**Backward compatibility**: existing `BatchRunResult` already has
`submitted`, `submitted_uncertain`, `failed`, `skipped_low_score`,
`dry_run_verified`, `cancelled`, `per_job`, `stop_reason`. D may
ADD fields (e.g. `consecutive_failures: int = 0`, `fatal_reason: str | None = None`) but must NOT rename or remove existing ones.

## Test isolation

Every test that creates a DB MUST use a `tmp_path` fixture per test.
The persistence test helpers (`_make_db`, `_seed`, `_row`) already do
this; the recovery/circuit/adapter-retry tests should mirror the same
pattern. Tests must never write to `/Users/sagarverma/Pictures/Claude-experiments/job-finder/jobs.db`.

`requires_live_seek`-marked tests are out of scope for parallel
workstreams.

## Build order

The 5 workstreams may proceed in parallel. Each agent runs its own
focused test subset at the end of its work; the orchestrator merges and
runs the full suite + cross-cutting integration tests in Phase 2.

The orchestrator commits the entire result as a single squashed commit
once Phase 2 is green.

## What this pass does NOT build

- The continuous scrape-then-apply daemon loop (job-finder's `while True`
  cycle). Documented in ADR-0008 as a deliberate omission.
- Score-cache reuse or tailored-PDF reuse (performance, Phase 3 optional).
- Any UI redesign. Existing screens read the new statuses verbatim.
