# ADR-0001: Inventory of existing assets and definition of the minimal product

Date: 2026-05-29
Status: Accepted (Phase 0 output)

## Context

We are replacing the GUI of an older Electron desktop product (at `autoapply.com.au/`) with a new, well-engineered desktop GUI for macOS and Windows, built around a proven Python Seek-automation engine (`job-finder/`). The engine is a black-box dependency and will not be modified. The old Electron product has reusable parts. Before any framework decision, we must know exactly (a) what the engine's contract is, (b) what genuinely survives from the old product regardless of framework, and (c) what the smallest shippable product actually is.

## Inventory: the engine (job-finder)

**What it is.** Python 3.10+ async codebase. Persistent Chromium profile drives Seek via Playwright + Patchright. Claude CLI provides scoring and document tailoring (using the user's Max subscription, not an API key). SQLite + per-year xlsx persistence. RotatingFileHandler logs to `bot.log`. LibreOffice is required at runtime for docx to pdf.

**Entry points that exist today.** The daemon (`main.py`) is an infinite loop and is not the right shape for a GUI. `test_seek_apply.py` and `apply_one_a11y.py` are single-shot scripts that exit. The clean library-level call site is `applicator.apply(job, resume_pdf, cover_pdf, candidate, page=None) -> "applied"` (raises on failure). Scoring and tailoring are separate calls: `matcher.score_job(job)` and `tailorer.tailor(job, tier)`. **No unified `apply_to_job(url, profile, session) -> Result` exists.** The GUI's engine adapter will compose these.

**Inputs the engine consumes.** (1) A `JobListing` dataclass (url, title, company, board, description, easy_apply). (2) A `candidate` dict (name, email, phone) from `config.yaml`. (3) Resume text from `assets/profile.txt` (module-level cached on first read). (4) A persistent Chrome user-data-dir at `sessions/seek_chrome_profile/` (this is the Seek session; cookies live here, not in code). (5) Env vars: `ANTHROPIC_API_KEY` (only needed for non-Seek boards via browser-use), `DB_PATH`, `OUTPUT_DIR`. (6) Implicit deps: Playwright Chromium, LibreOffice, `claude` CLI on PATH with an active login.

**How one application runs.** Caller invokes `applicator.apply()`. Dispatcher routes to `seek_apply.apply_seek_quick()`. A persistent Playwright Chromium opens the apply page in the existing user-data-dir, asserts not redirected to login, clears stale resumes from Seek's profile library, uploads new resume + cover letter PDFs, iterates up to 12 form steps filling fields (LLM-assisted recovery on validation errors), submits, verifies on the Applied Jobs page. Returns `"applied"` or raises a typed exception (`SeekApplyError`, `BoardBlockedError`, `ExternalApplyError`, `PermissionError`, `TimeoutError`).

**Progress and outcome signalling.** Progress is plain Python logging to `bot.log` and stdout. There is no callback hook, no JSON event stream, no queue. Outcome is the function return value + a typed exception class. The daemon additionally persists via `tracker.upsert_application(app)` (SQLite + xlsx side effect). The GUI adapter will need to add a progress callback shim by either subclassing the logger handler or wrapping each engine call with explicit stage events.

**Concurrency.** Single application at a time. `process_lock.seek_lock()` enforces OS-level fcntl exclusion on `sessions/seek/.lock`. Async (`asyncio.run`) per process. Parallel calls into `applicator.apply()` in the same process are not safe because the persistent Playwright context is shared module-state.

**Safety gates.** **There is no built-in dry-run and no `ALLOW_REAL_SUBMIT` gate.** If `applicator.apply()` reaches the final form step, it submits. Any safe-mode behaviour must be added by the GUI wrapper, either by stopping before `_submit()` or by introducing a feature flag in the adapter. The user's plan requires `ALLOW_REAL_SUBMIT` to gate real submission throughout Phases 2 to 3, so the adapter must own this gate.

**Embedding constraints relevant to the GUI.** The engine opens a visible Chromium window. It expects a writable cwd (`output/`, `sessions/`, `jobs.db` are cwd-relative). LibreOffice and Chrome paths are macOS-hardcoded (`/Applications/...`) and need a Windows branch. The Claude CLI subprocess adds about one second of overhead per call. Sessions expire silently and surface as `PermissionError` only mid-apply; the GUI should expose a "test Seek session" affordance. The persistent Chrome user-data-dir is not portable between machines.

## Inventory: the old Electron product (`autoapply.com.au/`)

**Reusable framework-agnostic.** The renderer is React 18 + Vite + Zustand + Tailwind + React Router, talks to a local FastAPI sidecar over HTTP + WebSocket. Supabase auth is implemented correctly (anon key in renderer env, access token in renderer memory, refresh token in Electron `safeStorage`, access token forwarded to sidecar via `POST /auth/session`). The Zustand store is well-typed and clean. The sidecar (`backend/`) is a well-structured FastAPI app with CRUD routes, a bot state machine with circuit breaker and offline retry queue (`core/engine.py`), aiosqlite migrations, and thin LLM-proxy wrappers. None of this code knows Electron exists.

**Electron-specific and disposable.** `electron/main.ts`, `preload.ts`, `tray.ts`, root `package.json` (electron-builder, electron-updater, electron-store), and the docs around Nuitka packaging are framework-specific. The patterns inside them (port-discovery temp file, bearer token via env var, health-check handshake, graceful SIGTERM with 5 s grace then SIGKILL, encrypted refresh-token storage) translate to any sidecar-based framework.

**Code-signing, notarization, auto-update.** Documented in `README.md` and `PACKAGING.md` but **not wired into CI**. There are no `.github/workflows/` for the desktop app. Apple Developer ID, Team ID, app-specific password, and Windows code-signing approach (SSL.com eSigner or DigiCert KeyLocker) are referenced as env-var names only. None of the signing identities or secrets are configured. The user's belief that a usable CI shape exists turns out to be wrong; only the marketing site (`site/`) has any CI. **Practical implication:** signing identities, if the user owns them, are reusable. Everything around them must be built.

**Business logic worth carrying.** The auth flow, the sidecar bot state machine, the SQLite schema and migrations, the OS-keychain wrapper, and the LLM-proxy client. Note also a critical duplication: the old `backend/automation/applicant.py` is an alternative Seek automation that diverges from `job-finder` in six documented ways (per `debug-log.md`). It is half-baked relative to `job-finder`. **The new app should use `job-finder` as the engine and discard `backend/automation/`.**

**`proxy-server/`.** Remote FastAPI service that brokers LLM calls and Stripe billing. Holds the real OpenAI/Anthropic keys, verifies Supabase JWTs, enforces subscription limits. Independent deployment; not part of the desktop client. Reusable as-is if we want subscription gating; can be deferred otherwise.

**Discard.** `infra/terraform/` (server infra, irrelevant), `site/` (separate marketing project), `scripts/dry_run_apply.py` and `login_and_pick.py` (debug artefacts), `debug-runs/`, `backend/data/encryption.py` (skeleton).

## Minimal product

The smallest set of screens and actions a user genuinely needs:

1. **Sign in.** Supabase phone + password, identical to the old flow. Refresh token to OS keychain, access token in memory, forwarded once to engine on sign-in.
2. **One-time Seek session setup.** Open Chromium, prompt user to log in to Seek manually (including OTP), close. This populates the user-data-dir. Re-runnable on session expiry.
3. **Profile.** Edit name, email, phone, and load the resume text file. Save back to disk in the format `job-finder` expects.
4. **Queue.** A single screen showing scraped jobs with title, company, match score, and Apply/Skip per row. No bulk operations; the engine runs one at a time anyway.
5. **Run with live progress.** Trigger one application against the selected job. Visible stages: peek, score, tailor, fill form, submit (or dry-run gate). Live log tail.
6. **Results.** A list of applications with status (applied, failed, skipped), per-job timestamp, output PDF paths, error message if failed. Read from the engine's SQLite + xlsx.
7. **Settings.** `ALLOW_REAL_SUBMIT` toggle (default off), match threshold, daily cap, Seek session "test now" button.

Everything else from the old product (onboarding wizard, Stripe subscription UI, tutorials, two-factor modal, dashboard widgets beyond results) is **cut from the minimal product** and can be added back later if user demand surfaces. Per the user's "no speculative abstractions" rule.

## Open questions for the GUI to answer (not the engine)

- Where does the GUI place `output/`, `sessions/`, `jobs.db`, `bot.log`? Each platform has its convention (macOS `~/Library/Application Support/AutoApply/`, Windows `%APPDATA%\AutoApply\`). The engine assumes cwd; the GUI must set cwd before invoking and pass `DB_PATH` and `OUTPUT_DIR` env vars accordingly.
- How does the GUI surface a Seek session expiry without losing in-flight work? Recommendation: detect `PermissionError` from `applicator.apply()`, mark the job `paused`, and route to the session-setup screen.
- Does `ALLOW_REAL_SUBMIT` live in the GUI or the engine adapter? Recommendation: the adapter. The engine stays untouched; the adapter wraps the final submit step in a feature-flag gate. Documented in ADR-0003 if it ships.

## Decision

We will treat `job-finder/` as a stable black-box dependency, wrap it behind a typed `EngineAdapter` interface owned by the new app, vendor it as a pinned, unmodified module, and build the minimal product described above. Anything not in the minimal product is out of scope until it earns its way in. Framework choice is deferred to ADR-0002.

## Consequences

- The new app vendors `job-finder/` (likely as a git submodule or copied subtree pinned to a commit). No edits to its files. Any engine-change need is escalated via a separate ADR rather than smuggled in.
- The adapter owns three things the engine does not have: a unified `apply_to_job` call, a progress callback, and an `ALLOW_REAL_SUBMIT` gate.
- The renderer-side React UI from the old product **may** carry forward if the framework choice is webview-based; otherwise it becomes a visual reference only. ADR-0002 decides this.
- We do not adopt the old `backend/automation/` Seek implementation. `job-finder` is the single source of truth for Seek automation behaviour.
