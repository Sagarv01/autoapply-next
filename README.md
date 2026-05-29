# AutoApply Next

## What this is

A PySide6 desktop GUI for macOS and Windows that drives the `job-finder` Seek
automation engine in-process. The engine is a Python async codebase that
already works; this project does not try to rewrite it. The GUI imports
`job-finder` directly from `vendor/job-finder/` and calls it as ordinary
Python, so there is no port handshake, no bearer-token negotiation, no
sidecar lifecycle to manage, and no second process to crash. The reason for
in-process over a webview-plus-sidecar architecture is in ADR-0002. The
reason the engine source is vendored unmodified, and why anything under
`vendor/` is off-limits to edits, is in ADR-0001.

## Status (2026-05-29)

What is working today:

- Safety gate (`src/autoapply_next/engine/safety.py`) with 9 contract tests
  in `tests/contract/test_safety_gate.py`. The gate monkey-patches
  `seek_apply._submit` at runtime and substitutes a dry-run path that ticks
  the terms checkbox, asserts the submit button is visible and enabled,
  takes a full-page screenshot, then raises `DryRunReached`. Real submit is
  only reached when `allow_real_submit=True`.
- PII scrubber with 14 contract tests in
  `tests/contract/test_pii_scrubber.py`. Redacts the candidate's name,
  email, phone, and Seek URLs from log output before logs hit disk or
  Sentry.
- Qt worker threading model from ADR-0003: an `EngineWorker(QObject)`
  pinned to a `QThread`, owning its own asyncio loop, communicating with the
  GUI via Qt signals (`started`, `progress`, `finished`, `failed`) and a
  cooperative cancellation path. 4 pytest-qt tests in
  `tests/ui_tests/test_worker_signals.py` cover signal emission, queued
  cross-thread invocation, and `asyncio.Task.cancel()` teardown.
- End-to-end live-Seek dry-run integration test
  (`tests/integration/test_live_seek_dryrun.py`, marker
  `requires_live_seek`) that drives peek to score to tailor to apply to
  gate to screenshot against a real Seek quick-apply listing. Verified
  2026-05-29 at 238 seconds wall-clock; the run log is in
  `docs/test-log.md`.

What is unfinished:

- The Supabase sign-in screen
  (`src/autoapply_next/ui/signin_screen.py`) renders, but the sign-in
  button is currently a Skip placeholder. The Supabase SDK is in
  `pyproject.toml`; the auth flow itself is not wired up. Out-of-scope for
  the slice that ships the dry-run.
- The in-app Seek session bootstrap
  (`src/autoapply_next/ui/session_setup_screen.py`) currently directs the
  user to run the engine's CLI script to populate the persistent Chrome
  user-data-dir. The button-driven flow that opens Chromium, prompts for
  manual login including OTP, then closes the browser, is not yet
  implemented.
- The scraper is not wired into the Queue screen
  (`src/autoapply_next/ui/queue_screen.py`). The screen renders the layout
  but does not yet call the engine's job-board scraper to populate rows;
  job URLs are currently entered manually.
- Code-signing identities and secrets. The signing scripts
  (`packaging/macos/sign-notarize.sh` and `packaging/windows/sign.ps1`)
  exist, the CI workflow at `.github/workflows/release.yml` references the
  secret names, but no certificates are provisioned. The shopping list is
  in `packaging/CERT_CHECKLIST.md`.
- The tufup auto-updater wiring. `tufup>=0.10` is in the `dev` extras and
  the strategy is decided in ADR-0006, but the root-key ceremony has not
  been performed and the updater module
  (`src/autoapply_next/updater/`) is empty.
- Windows testing. The codebase is built for Windows from day one (path
  handling in `src/autoapply_next/platform/paths.py`, the
  `packaging/windows/sign.ps1` script, the Windows job in
  `.github/workflows/release.yml`) but development happened on macOS. The
  Windows runner in CI is the verification path for the Windows slice;
  manual smoke tests on a Windows box have not been run.

## Architecture in one diagram

```
+--------------------------------------------------------------+
|                       Qt main thread                         |
|                                                              |
|  MainWindow                                                  |
|  +-- SignInScreen                                            |
|  +-- SessionSetupScreen                                      |
|  +-- ProfileScreen                                           |
|  +-- QueueScreen                                             |
|  +-- RunScreen           <--- listens on Qt signals          |
|  +-- ResultsScreen                                           |
|  +-- SettingsScreen      <--- toggles ALLOW_REAL_SUBMIT      |
|                                                              |
+----------------------------+---------------------------------+
                             |
                             |  QMetaObject.invokeMethod
                             |  (queued connection, serialized)
                             v
+--------------------------------------------------------------+
|              EngineWorker (QObject on a QThread)             |
|                                                              |
|  asyncio.new_event_loop()  pinned to this thread             |
|  loop.run_until_complete( apply_to_job(...) )                |
|                                                              |
|  emits: started, progress(ProgressEvent),                    |
|         finished(ApplicationResult), failed(str)             |
|  reads: is_cancelled  (cooperative, between stages)          |
|                                                              |
+----------------------------+---------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|         autoapply_next.engine.adapter.apply_to_job           |
|                                                              |
|  Composes the engine: peek -> score -> tailor -> apply.      |
|  Installs EngineHooks (captures cover-letter text and        |
|  screening answers via the engine's journal file) and the    |
|  SafetyGate (monkey-patches seek_apply._submit and            |
|  tailorer._export_cover_letter_pdf).                         |
|                                                              |
+----------------------------+---------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|              vendor/job-finder/  (DO NOT EDIT)               |
|                                                              |
|  applicator.apply, matcher.score_job, tailorer.tailor,       |
|  seek_apply.apply_seek_quick, seek_apply._submit,            |
|  seek_apply.peek_is_quick_apply, seek_apply.fetch_seek_jd    |
|                                                              |
|  Persistent Chrome user-data-dir lives at:                   |
|    engine_workdir/sessions/seek_chrome_profile/              |
|  Cookies and the Seek session are on disk, not in code.      |
+--------------------------------------------------------------+
```

## Running it locally

```
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
python -m autoapply_next
```

The engine has two binary dependencies that are not pip-installable and
must be on PATH before the apply flow can run end-to-end:

- LibreOffice (`soffice` on PATH). The tailor stage produces a docx and
  converts it to pdf via `soffice --headless`. Without LibreOffice the
  tailor stage fails and the run stops before the apply stage.
- The Claude CLI (`claude` on PATH) with an active Max subscription. The
  matcher and tailorer call `claude` as a subprocess. Without it the
  score and tailor stages fail.

On macOS LibreOffice installs to `/Applications/LibreOffice.app/`, which is
the path the engine looks for; `brew install --cask libreoffice` is the
fastest route. The Claude CLI is `npm i -g @anthropic-ai/claude-code` then
`claude login` once.

## Tests

```
pytest tests/contract tests/ui_tests              # always run
pytest tests/integration -m requires_live_seek    # opt-in; needs live Seek session
```

The first command is what CI runs. It covers the safety-gate contract, the
PII scrubber, and the Qt worker signal model. No network, no real browser,
no Seek login required; runs in a few seconds.

The second command is opt-in via the `requires_live_seek` pytest marker
declared in `pyproject.toml`. It hits real Seek listings with the real
persistent Chrome session, so it can only run on a developer machine that
has logged into Seek manually and populated
`engine_workdir/sessions/seek_chrome_profile/`. CI does not run it, by
design: a credentialled live-Seek run on every push would be the wrong
posture for a safety-critical automation. The iterate-fix loop and the
2026-05-29 run log are in `docs/test-log.md`.

## Building releases

The release pipeline is split across:

- `packaging/pyinstaller.spec`: the one-dir PyInstaller bundle definition
  (per ADR-0005). Cross-platform; the spec branches on `sys.platform` for
  macOS-vs-Windows specifics.
- `packaging/macos/sign-notarize.sh`: signs the macOS bundle with
  `codesign --options runtime`, submits to Apple via
  `notarytool submit --wait`, then staples with `xcrun stapler`. Reads the
  Apple identity from the env vars listed in `packaging/CERT_CHECKLIST.md`.
- `packaging/windows/sign.ps1`: signs the Windows binary with `signtool`,
  either via a locally imported PFX (Option A, SHA1 thumbprint) or via
  SSL.com eSigner cloud HSM (Option B). The script branches on
  `WIN_SIGNING_USE_ESIGNER`.
- `.github/workflows/release.yml`: tag-triggered (`vX.Y.Z`), builds on
  macOS arm64, macOS x64, and Windows x64 runners, calls the signing
  scripts, then attaches the artefacts to a GitHub Release.

The full shopping list of certificates, signing identities, and tufup keys
the workflow expects is in `packaging/CERT_CHECKLIST.md`. None of those
secrets are provisioned in this repository yet; that is an operator task
the user does once.

Without those secrets the CI still produces an artefact, but each signing
script detects the missing variables, prints `MISSING_CERTS: <NAME>`,
exits 0 with an unsigned binary, and the workflow uploads it labelled
`DEV BUILD`. Users on such a build see Gatekeeper or SmartScreen warnings
on first launch.

## Engine workdir

The engine reads and writes a directory it calls the workdir. In
production the GUI computes the workdir via
`autoapply_next.platform.paths.engine_workdir()`, which on macOS resolves
to `~/Library/Application Support/AutoApply Next/engine/` and on Windows to
`%APPDATA%\AutoApply Next\engine\`. During development you point the app at
a different workdir by setting the `AUTOAPPLY_NEXT_ENGINE_WORKDIR`
environment variable before launch.

What lives in the workdir:

- `config.yaml`: candidate dict (name, email, phone), match threshold,
  daily cap. The adapter refuses to start if this is missing.
- `assets/`: `profile.txt` (the resume text the engine reads at module
  load), plus the source `.docx` template the tailorer fills.
- `sessions/seek_chrome_profile/`: the persistent Chrome user-data-dir
  that holds the Seek cookies. Created by the one-time session-bootstrap
  flow; not portable between machines.
- `output/`: tailored resume PDFs, cover-letter PDFs, dry-run screenshots
  in `output/dryrun-screenshots/`.
- `jobs.db`: the engine's SQLite database (seen jobs, applications,
  outcomes). Schema is owned by the engine.
- `errors/`: per-application JSONL journal that `EngineHooks` reads to
  capture screening answers and cover-letter text.
- `bot.log`: the engine's RotatingFileHandler output. Scrubbed by the PII
  filter before anything leaves the local machine.

## Safety gate, in plain language

`ALLOW_REAL_SUBMIT` is the single boolean that distinguishes a dry-run from
a real Seek application. The gate is implemented as a runtime monkey-patch
on `seek_apply._submit`; with `ALLOW_REAL_SUBMIT=False` the patched
`_submit` ticks the terms checkbox, asserts the submit button is visible
and enabled, takes a screenshot, and raises `DryRunReached` instead of
clicking. With `ALLOW_REAL_SUBMIT=True` the patched `_submit` delegates to
the original.

The flag defaults `False` in the adapter, in the Settings store, in the
test suite, and in the CI workflow. The only path that sets it `True` is
the user toggling it in the Settings screen
(`src/autoapply_next/ui/settings_screen.py`). The framing from ADR-0004 is
exact: "this gate is the only thing preventing real applications going to
real employers under my real name."

## Decisions

- `docs/adr/0001-inventory-and-minimal-product.md`: vendor `job-finder` as
  an unmodified pinned dependency; wrap behind a typed `EngineAdapter`;
  ship the seven-screen minimal product (sign-in, session setup, profile,
  queue, run, results, settings); cut everything else.
- `docs/adr/0002-framework-decision.md`: PySide6 with PyInstaller,
  in-process with the engine. Tauri and Electron evaluated; PySide6 wins
  on integration friction because the engine is Python and a sidecar
  boundary is unnecessary tax.
- `docs/adr/0003-threading-model.md`: dedicated `QThread` engine worker
  owning its own asyncio loop, communicating with the GUI via Qt signals;
  cooperative cancellation between stages plus `asyncio.Task.cancel()`
  in-flight. The worker is a singleton because the safety gate is
  process-global state.
- `docs/adr/0004-safety-gate-seam.md`: monkey-patch `seek_apply._submit`
  at the engine module level; in dry-run, replace it with a function that
  proves submit-readiness then raises `DryRunReached`; in real-submit,
  delegate to the original. The gate's selector list is kept in sync with
  the engine by a contract test that fails loudly on drift.
- `docs/adr/0005-packaging.md`: PyInstaller one-dir bundles; macOS signing
  via `codesign` + `notarytool` + `stapler`; Windows signing via
  `signtool` with either a local PFX or SSL.com eSigner; Chromium fetched
  on first run, not bundled. Unsigned dev artefacts when secrets are
  missing.
- `docs/adr/0006-auto-updater.md`: `tufup` for signed in-app updates over
  TUF (root, targets, snapshot, timestamp keys); GitHub Releases as the
  feed; 24-hour startup poll plus a manual check in Settings; updates
  staged then swapped on next launch.

## Repository layout

```
autoapply-next/
  README.md                          this file
  pyproject.toml                     hatchling build, deps, pytest config
  docs/
    adr/                             one decision per file, numbered
    test-log.md                      iterate-fix loop, live-Seek runs
  src/autoapply_next/
    __main__.py                      entry point; `python -m autoapply_next`
    auth/                            Supabase sign-in (skip button today)
    engine/                          adapter, hooks, progress, results,
                                       safety, worker
    platform/                        paths.py: per-OS workdir, log dir
    safe_logging/                    PII scrubber + structured logging
    ui/                              7 screens + MainWindow + settings_store
    updater/                         tufup integration (empty today)
  packaging/
    pyinstaller.spec                 one-dir bundle, cross-platform
    macos/sign-notarize.sh           codesign + notarytool + stapler
    windows/sign.ps1                 signtool, PFX or eSigner branches
    CERT_CHECKLIST.md                shopping list of certs and keys
  resources/                         icons, qrc files
  tests/
    contract/                        safety-gate (9), PII scrubber (14)
    ui_tests/                        Qt worker signals (4), pytest-qt
    integration/                     requires_live_seek dry-run end-to-end
  vendor/
    VENDOR_MANIFEST.md               pinned commit, vendoring rules
    job-finder/                      the engine, untouched
  .github/workflows/release.yml      tag-triggered signed release
```

Hard rule: do not edit anything under `vendor/`. The engine is a black-box
dependency per ADR-0001. If the engine needs a change, escalate via a new
ADR and a fork-and-pin, not an in-place patch.
