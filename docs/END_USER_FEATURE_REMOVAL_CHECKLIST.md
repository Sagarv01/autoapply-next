# End-User Feature Removal Checklist

Goal: keep the tester build powerful for diagnosis while making the end-user
build feel like a simple product, not an internal test harness.

## Already Hidden In End-User Mode

- [x] Raw Profile editor screen.
- [x] Single-URL Run screen.
- [x] Per-job "Run dry-run" row buttons in Queue.
- [x] Pacing/throttle override in Settings.
- [x] Real submission gate in Settings.
- [x] `DRY-RUN` / `LIVE SUBMIT` status-bar badge.
- [x] `DRY-RUN` / `LIVE SUBMIT` progress-screen mode badge.
- [x] Engine workdir path in the status bar.

## Remove Or Replace Before User Launch

- [ ] Replace tester submission controls with one product-level policy for user
      builds: either guided review-before-submit or a clear onboarding consent
      flow. Do not expose `ALLOW_REAL_SUBMIT`, "dry-run", or "live submit" words
      to end users.
- [ ] Remove developer words from user-facing copy: engine, worker, proxy,
      scraper, dry-run, ALLOW_REAL_SUBMIT, threshold, SQLite, and workdir.
- [ ] Simplify Queue columns to the decisions users care about: role, company,
      fit, status, and next action.
- [ ] Hide raw URLs unless the user explicitly opens job details.
- [ ] Replace technical errors with short recovery messages and keep traceback
      details only in tester logs.
- [ ] Rename Batch copy to plain progress language such as "Applying" or
      "Applications in progress".
- [ ] Keep STOP visible during automation, but label it as a plain cancel/stop
      control rather than an engine control.
- [ ] Review Settings and keep only user decisions: daily cap, job fit minimum,
      account/session, billing, and notifications.
- [ ] Move advanced diagnostics, manual reload actions, database paths, and
      verbose state text to tester mode only.
- [ ] Verify no user-mode screen says "Tester", "AutoApply Next", "Run dry-run",
      or "Real submission gate".

## Keep For End Users

- [ ] Setup/onboarding.
- [ ] Seek session connection.
- [ ] Job search and apply queue.
- [ ] Application progress with stop control.
- [ ] Waiting-on-you questions.
- [ ] Results/history.
- [ ] Upgrade and billing.
- [ ] Simple Settings.
