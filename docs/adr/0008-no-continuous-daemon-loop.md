# ADR-0008: No continuous-daemon loop, on purpose

Date: 2026-05-31
Status: Accepted

## Context

job-finder's `main.py:289-413` is a continuous `while True` daemon: it scrapes, pre-scores, queues, applies one by one with retry and 60-120s gaps, closes the Chromium session, sleeps `interval ± jitter`, and repeats indefinitely, auto-flipping date/relevance sort when a cycle is dry. autoapply-next as of this branch processes one user-curated batch per Submit click and stops; it does not auto-scrape or auto-loop. The launch-readiness audit at `docs/audit-...` (kept in the commit log under HEAD `362e317`'s descendants) flagged this gap explicitly and asked for the resilience baseline (sleep prevention, single-instance lock, log rotation, recovery, pacing, circuit breaker) to be lifted from job-finder into autoapply-next without lifting the daemon loop itself.

## Decision

autoapply-next stays a single-batch tool. The user reviews and approves; the worker iterates the approved set with pacing and a circuit breaker; the worker stops when the set is exhausted or the breaker trips. The continuous daemon is not built in this pass. The resilience layer that job-finder needed in order to run unattended for hours, however, is lifted in full: Mac sleep prevention via `caffeinate`, single-instance lock via the vendored `process_lock.seek_lock`, `RotatingFileHandler` log rotation, `recover_orphans` reconcile-on-start for crashed mid-applies, an `in_progress` status written before the engine's submit click, the `(60, 120)` randomized inter-job throttle (matching `APPLY_GAP_MIN/MAX`), a fatal-condition classifier that halts the batch on session expiry or CAPTCHA and leaves remaining jobs `queued`, and a consecutive-failure breaker. Within those guard rails a single curated batch is safe to start and walk away from; that is the product. Adding the daemon loop on top would re-introduce two questions we explicitly do not want to answer right now (when does the loop pre-screen at all, and how does the human-review step compose with continuous operation) and would benefit from its own design pass.

## Consequences

A user who wants "press start, the bot runs forever" still has job-finder, which is unmodified at `vendor/job-finder/`. The autoapply-next user gets the review-then-run model with the daemon's resilience but not its autonomy. A future ADR can introduce an autorun mode (e.g. a Settings toggle that, after a successful batch, automatically triggers a scrape on the previous keyword and a new prepare, then waits for the user's approval before running) without contradicting this decision. Anything that needs the daemon's exact semantics today should run the daemon today; autoapply-next is not a drop-in replacement and explicitly should not be marketed as one. The implementations of `recover_orphans`, the circuit breaker, and the worker tally are designed so that adding an outer loop later does not require redesigning them: the batch runner already returns a tally that survives mid-flight cancel, the persistence layer already exposes the eligibility filter, and the lock guards a process not a loop iteration.
