# AutoApply Next: production sprint task ledger

Single source of progress truth. Work strictly from this file, top to bottom.
Mark `[x]` only when a task's tests pass. Mark `BLOCKED: <reason>` and move on
if blocked per escalation rules. Run the full regression suite at the end of
every phase; any red stops new feature work until fixed.

## ====== SHIPPABLE USER-READY MISSION (2026-06-16) — CURRENT WORK ======
Ship THIS app (PySide6/autoapply-next). The Electron app (autoapply.com.au/{electron,renderer,backend})
is ARCHIVED under `autoapply.com.au/_deprecated_electron_app/`. `job-finder` is the user's personal
engine + ground-truth — leave it alone. The proxy (`autoapply.com.au/proxy-server`, deployed at
api.autoapply.com.au) + Supabase/Stripe/Resend/PostHog carry over unchanged; this Qt app uses the same proxy.
No real submit during dev (`allow_real_submit`=False). Suite green (300 baseline).

**Phase B APPROVED**: keep all 19 screening fields; EEO (gender/aboriginal/disability/veteran) default
"Prefer not to say"; include work-arrangement(13)/highest-education(14)/employment-status(15) as fields
wired into where the engine currently guesses (Claude fallback). Conditional visa fields (type+expiry)
shown only when not citizen/PR.

**PySide6 remaps (not Electron):** Stripe checkout via `QDesktopServices.openUrl`; Stripe return via a
loopback HTTP listener on a random localhost port set as success_url (fallback: poll Supabase tier on
app focus) — NOT an autoapply:// deep link; packaging via PyInstaller (extend existing) for mac+win;
v1 = signed installers + in-app version check linking to download (defer silent auto-update); all UI is Qt.

**Tier model (Phase E):** Free 50 apps / Basic unlimited no-tailoring / Pro unlimited + per-job tailoring.
Server-side enforcement at the proxy (already gates tailoring + the 50 cap; needs free/starter/pro ->
Free/Basic/Pro reconciliation + Resend + PostHog). STOP for Stripe Price IDs.

**Gap vs phases (re-audit 2026-06-16, file:line):**
- A onboarding: PARTIAL. `ui/main_window.py:71-116` is free-nav (no gate); `ui/signin_screen.py` is a STUB.
  Need: real Supabase auth + sequential wizard + unlock gate. Reuse profile/session/criteria screen logic.
- B fields: `ui/profile_screen.py` exists; extend to the 19 fields.
- C held queue: ABSENT (net-new). Intercept unknown screening Qs before the engine guesses
  (`vendor/job-finder/seek_apply.py:923-938,2173-2231`) -> "Waiting on you" queue -> answer -> unblock+resume + Resend nudge.
- D run UX: PARTIAL. `engine/progress.py` 8 stages + `batch.py` jitter exist; add state vocabulary,
  time-of-day pacing (weekday 09:00-17:00=15-45s else 1-3min), invisible 100/day cap, 3 cooldown messages.
- E paywall: ABSENT in UI (net-new Qt). Proxy backend ~ready.
- F packaging: PyInstaller .app exists; extend Windows + version-check + signing (STOP for certs).

**MISSION TASK LEDGER (work top-to-bottom; commit per unit; STOP only for Stripe Price IDs / signing certs / genuine product decision):**
- [ ] M0 Phase 0 audit + archive Electron + corrected ledger. **DONE** (this entry).
- [~] M-A Onboarding wizard gating the bot. **Logic foundation DONE (tested):**
  - [x] M-A.1 auth: `auth/{supabase_auth,token_store,manager}.py` (GoTrue + keychain + proxy token wiring). 12 tests. commit bd2a430.
  - [x] M-A.2 profile facts: `onboarding/profile_facts.py` (19 fields, conditional visa, EEO defaults, config.yaml store). 7 tests. commit 848e300.
  - [x] M-A.3 gating state: `onboarding/state.py` (5-step completion + persisted flags; locks bot until set up). 7 tests. commit 03c48ef.
  - [~] M-A.4 Qt UI. **Threading pattern + sign-in DONE:**
    - [x] `ui/async_task.AsyncTaskRunner` — reusable off-UI-thread runner (own asyncio loop in a daemon thread, gate-free; submit(fn)=sync via to_thread, submit_coro(coro)=async; succeeded/failed signals + token). THE pattern every blocking wizard call reuses (profile save, upload, criteria save, entitlement, checkout). 5 qtbot tests. commit 352f964.
    - [x] Real SignInScreen (email+password, AuthManager, background auth, error states, validation) wired into MainWindow gate (one shared AuthManager+runner; runner.stop() in closeEvent). 6 qtbot tests. commit c97ab9d. HANDOFF click-through checklist started (79d989a).
    - [x] Phase A wizard COMPLETE: ProfileFactsScreen (19 fields, conditional visa, EEO defaults, off-thread save) d01ce3c; DocumentsScreen (resume req + cover opt, off-thread copy) 974241a; CriteriaScreen (skills+location) + AcknowledgeScreen (honesty line) 161c23f; OnboardingWizard (routes first_incomplete_step, advances per step, emits completed) + MainWindow gate (locks queue/run/batch/results until is_onboarding_complete; off-thread auth.restore at startup) e246895. Adversarial UI-thread-safety review (3 lenses) — off-thread widget access CLEAN; fixed restore-failed gate + clean task drain 6da7f24. 40+ qtbot tests.
    - [x] Paywall: BillingScreen (entitlement fetch + run_checkout via submit_coro + QDesktopServices.openUrl; upgraded/pending/canceled outcomes) 9ab6acf. **NOT yet wired into MainWindow stack/toolbar.**
    - [x] UI TAIL DONE: "Waiting on you" HeldQueueScreen (answer + remember in bank + requeue) a0e5b8a; persistence.requeue_held_jobs + screening.answer_flow.answer_and_unblock e417efc; BillingScreen+HeldQueueScreen wired into MainWindow (+ NeedsProError surfaces upgrade, held count badge) 55bc992; RunStatusWidget + run_batch on_cooldown hook + worker cooldown_started + batch-view rendering b198d83. ~25 qtbot/contract tests.
    - [B] Resend nudge (email when a question is waiting): **BLOCKED — Resend ABSENT in proxy; needs a Resend API key + a proxy email endpoint.**
    - [B] Free apply tailoring behavior: **PRODUCT DECISION — does Free/Basic apply with BASE docs (skip tailoring, no 403) or attempt tailoring and get upsold on 403? Engine currently always tailors -> a Free user's apply would 403 every time. Tier model says "base docs as-is".**
    - [ ] HUMANIZE pass on ui/run_status.py + screen draft strings (user does this).
- [~] M-C Unknown-question held queue. **Logic core DONE (tested, 42 tests):**
  - [x] M-C.1 `screening/held_queue.py` (HeldQueue: dedup-by-normalized-question, job→question waiting graph, answer-unblocks-resume, remembered answers, JSON persistence). 11 tests. commit 4d9f364.
  - [x] M-C.2 `screening/resolver.py` (answered/delegate/held policy; is_answerable_from_facts canonical matcher) + `screening/interceptor.py` (wraps seek_apply._claude_answer @923-938, no vendor edit; remembered→return, fact→original, unknown→persist+raise QuestionHeldError). 31 tests. commit 5e4d579.
  - [~] M-C.3 INTEGRATION. **Backend DONE:** ApplicationStatus.HELD + map_status('held', non-terminal); adapter installs ScreeningInterceptor + catches QuestionHeldError→HELD (QuestionHeldError is BaseException so the vendored `except Exception` can't swallow it); run_batch tallies .held (not failure/not submit). commit 4e37c4a. **Remaining (Qt sub-batch):** "Waiting on you" UI; answer-unblocks re-apply (flip 'held'→'queued'); Resend nudge (Resend absent in proxy).
- [~] M-D Run UX. **Backend DONE:** pacing gate wired into run_batch chokepoint (weekday 09:00-17:00=60-90s, else 60-180s, never below the 60s floor; invariant test green) commit 52b1c12/f61afff; invisible 100/day cap per-day-across-runs (persistence.count_today_submissions + worker.daily_cap_kwargs) commit 212e124; run-state vocabulary + copy drafts (RunState, 3 cooldown msgs, honesty line, Waiting-on-you prompt) commit 74a730c. **Remaining (Qt sub-batch):** render states in main UI; keep-polling on daily_cap_reached.
- [x] (off-batch) Supabase key migration: proxy → sb_secret_ (config.supabase_server_key, opaque, no JWT decode) ed078d4; client → sb_publishable_ 1b337ef; HANDOFF pre-launch checklist 917db11. Pre-launch: provision sb_secret_ in SSM for prod webhook writes.
- [~] M-E Paywall + entitlement. **Code-complete + unit/integration-proven (proxy 86 + client tests); BLOCKED on Stripe test-mode access:**
  - [x] M-E.1 server tier model `proxy-server/tiers.py` (Free/Basic/Pro; legacy starter→basic) wired into auth gate (per-tier app_cap + requires_tailoring 403 needs_pro), routes/llm (/tailor-resume+/cover-letter Pro-gated; /complete task hint), webhook _tier_for_price + checkout _resolve_price_id, config STRIPE_PRICE_BASIC_*. commit 080eccd. Webhook→tier integration test 8bdbe21.
  - [x] M-E.2 client: llm_proxy task="tailor" + NeedsProError; ProxyLLM per-module wrapper (only tailorer tailors). commit 7c6e019.
  - [x] M-E.3 proxy checkout honors loopback success/cancel URLs (localhost-only). commit 9b08918.
  - [x] M-E.4 client billing/ pkg: LoopbackReturnServer + proxy_billing + checkout_flow.run_checkout (start→create→open→wait→poll tier flip; always stops). commit 597b248.
  - [x] M-E.5 real Stripe TEST products/prices + money-path E2E **DONE 2026-06-16** (test key provided; drove Stripe test API directly since MCP is live). Created Basic+Pro products+prices (livemode=false; IDs in proxy-server/STRIPE_TEST_IDS.md). Proven green: real checkout session vs Pro price w/ loopback URLs; real webhook handler (HMAC sig-verified) maps real Pro price→tier=pro; real Supabase row flipped Free→Pro then restored; real gate blocks Free (403 needs_pro) + unlocks Pro. Only stub: stripe.Subscription.retrieve. Remaining: Resend + PostHog absent; Qt upgrade UI pairs w/ UI batch; deployed-proxy webhook delivery needs SUPABASE_SERVICE_ROLE_KEY.
- [ ] M-F Packaging mac+win (PyInstaller) + in-app version check + signing. STOP: Apple + Windows EV certs.

Branch: `production-sprint` (off `user-ready-sprint`). Do not merge to main.
Status legend: `[ ]` todo, `[x]` done (tests pass), `[~]` in progress,
`[B]` blocked.

Protected invariants (tests may never be weakened/skipped/deleted): throttle
floor on every path incl. single-job loops; same-role dedup hard block; verifier
company-must-match; score threshold single-sourced+honored everywhere incl.
review mode and pilots; no submit while kill switch active; no submit with red
preflight.

---

## Phase 1: Proxy production hardening (autoapply.com.au/proxy-server)

- [x] 1.1 Task-scoped endpoints `/v1/score`, `/v1/generate-docs`, `/v1/screening-answer`; proxy owns prompts, model routing, per-task max-token caps; NO raw passthrough. **Acceptance:** each endpoint accepts a typed task payload (not raw prompts), selects model + caps server-side; a request attempting raw-prompt passthrough is rejected; unit tests per endpoint with a mocked Anthropic backend. **DONE**: the 5 typed task endpoints (`/api/llm/score-job|tailor-resume|cover-letter|screening|screening-one`) already enforce typed payloads + server-side model/caps. Added the generic engine seam `/api/llm/complete` per D1 (`routes/llm.py` + `anthropic_service.complete`): sends `system`/`user` verbatim but with a server-side model allowlist (sonnet/haiku only, else 400 `model_not_allowed`) + max_tokens clamp to 4096; auth-gated + metered (endpoint `complete`) but NOT counted against the application quota. 9 tests (`tests/test_llm_complete.py`); full suite green (26).
- [x] 1.2 JWT validation against Supabase JWKS, local verification, 401 on expiry. **Acceptance:** valid token passes; expired/invalid/wrong-issuer returns 401; JWKS fetched+cached; unit tests for each case. **DONE + LIVE VERIFIED 2026-06-15**: code in `auth.py` (JWKS ES256/RS256, HS256 fallback). Live: a real Supabase ES256 access_token (minted via admin-create + password grant on `ndkeryoqlvktzuzxubvb`) validated through the locally-run proxy and reached the model (HTTP 200); no-token -> 401. Project uses asymmetric JWT so no JWT_SECRET needed.
- [ ] 1.3 Entitlements table in Supabase Postgres, written by Stripe webhooks (checkout.session.completed, customer.subscription.updated/deleted, invoice.payment_failed). **Acceptance:** webhook handler upserts entitlement per event type; signature-verified; idempotent; integration test with Stripe test webhook events / test clock.
- [x] 1.4 Proxy reads entitlements with a 5-minute cache. **Acceptance:** active entitlement allows routing; inactive/expired denies (402/403); cache TTL=5min verified; test that a webhook update is reflected within TTL. **DONE** (proxy commit 27124e8): cache + gating + webhook invalidation already existed; aligned TTL 60s->300s per D4, now runtime-configurable via `SUBSCRIPTION_CACHE_TTL_SECONDS` (`auth._cache_ttl`). 11 tests (TTL default/override/fallback, cache-hit-skips-refetch, expiry-refetch, invalidate-reflects-webhook-within-TTL, active-allows, inactive-denies-402 x4 statuses). Suite green (37).
- [x] 1.5 Per-user rate limits + daily token quotas enforced BEFORE routing. **Acceptance:** over-limit request rejected (429) before any Anthropic call; quota resets daily; unit tests. **DONE** (proxy commit 669e870): per-minute request limiter already existed (`rate_limit.py`); added the missing daily TOKEN quota in `token_quota.py` (in-memory per `(user_id, UTC date)`), checked in the auth gate (`require_active_subscription`) so an over-budget user gets 429 before any Anthropic call; `record_usage` bumps the counter; resets at UTC midnight; `DAILY_TOKEN_QUOTA` env (default 2M, <=0 disables). 10 tests incl. end-to-end "429 before Anthropic". Suite green (47).
- [x] 1.6 Usage metering: tokens per user per task logged to Postgres. **Acceptance:** every completed call writes a usage row (user, task, input+output tokens, ts); test asserts a row written per call. **DONE** (proxy commit d4fe9d8): write path (`record_usage`->`insert_usage_log`) already existed; added the acceptance test (a row per call + daily-counter bump + quota increment only when flagged). Suite green (49). Live row-in-Postgres verification BLOCKED on Supabase keys.
- [x] 1.7 Remote config endpoint: global submission kill switch + minimum client version; reject clients below the floor. **Acceptance:** `/v1/config` returns kill-switch + min-version; a request with a below-floor client version header is rejected; tests. **DONE** (proxy commit 2429aa7): `routes/config.py` GET `/api/config` + `enforce_min_client_version` dependency on every `/api/llm/*` route (426 below floor, before token spend); 13 tests, proxy suite green (17).
- [x] 1.8 Anthropic API key in AWS SSM/Secrets Manager, never in repo or bundle. **Acceptance:** proxy loads the key from SSM/Secrets at runtime; grep confirms no key literal in repo; test (mocked SSM) for the loader. **DONE** (proxy commit e80228b): `services/secrets.py` + config wiring, gated on `AUTOAPPLY_SECRETS_SSM_PREFIX`; 4 mocked tests + live-verified against real AWS SSM. The real key VALUE is provisioned at deploy (BLOCKED on the operator supplying the Anthropic key).
- [~] 1.9 Phase-1 tests: unit + integration with a mocked Anthropic backend, then ONE real call per endpoint against staging. **Acceptance:** mocked suite green; one real staging call per endpoint succeeds and is logged. **PARTIAL (LIVE) 2026-06-15**: mocked suite green (49). Real call done for the engine-critical `/api/llm/complete` (real Anthropic sonnet completion, logged to usage_log). Remaining: one real call each for the 4 task endpoints (score-job is OpenAI -> needs OPENAI_API_KEY; tailor-resume/cover-letter/screening are Anthropic, can verify now). Local proxy off SSM; repeat against the EC2 staging URL once deployed.

## Phase 2: Engine migration to the API

- [x] 2.1 Replace every claude CLI call with proxy-client calls (via an adapter-layer monkey-patch of the engine's LLM seam; do NOT edit vendor/). **Acceptance:** matcher/tailorer/seek_apply LLM calls route through the proxy client; zero CLI invocations remain on the apply path. **DONE** (app commit 3fcd692): `engine/llm_proxy.py` (claude_complete-shaped client -> proxy `/api/llm/complete`, typed ProxyError hierarchy, JWT + X-Client-Version) + `engine/llm_adapter.ProxyLLM` (context manager swapping the bound `claude_complete` in matcher/tailorer/seek_apply, no vendor edit), wired into `apply_to_job`/`score_job_only`/`tailor_only`. 13 mocked seam tests; full app suite 261 green. Live JWT end-to-end BLOCKED (verified by 2.4 no-CLI test next).
- [x] 2.2 Proxy-client failure handling: 401 refresh-and-retry-once, timeouts, retry policy per failure class, kill-switch response (halt submissions, surface in UI). **Acceptance:** unit tests per failure class; 401 triggers exactly one refresh+retry; kill-switch halts submission and emits a UI signal. **DONE** (app commit 970e32c): `_proxy_claude_complete` does 401 refresh-and-retry-once via a pluggable refresher; `llm_proxy.submissions_enabled()` polls the public `/api/config` kill switch (fail-open) + `KillSwitchError`; `persistence.is_fatal_condition` halts the batch + surfaces a reason for AuthExpired/SubscriptionExpired/ClientTooOld/QuotaExceeded/KillSwitch (transient ProxyUnavailable stays retryable). 13 tests; suite 274 green. UI signal wiring of kill-switch lands in Phase 5 preflight (KILL_SWITCH_ACTIVE).
- [B] 2.3 Output quality on the golden test set: identical or better. **Acceptance:** golden-set scores/docs match or beat the CLI baseline; documented comparison. **BLOCKED**: parity needs a real ANTHROPIC_API_KEY (proxy must make real model calls to compare against the CLI baseline). Harness can be scaffolded once the key lands; CLI baseline capture also needs claude on PATH.
- [x] 2.4 Engine runs with claude CLI absent from the machine. **Acceptance:** with `claude` removed from PATH, a dry-run apply completes via the proxy. **DONE** (app commit 83840fc): `requires_engine` test removes claude from PATH, guards against any `claude` subprocess spawn, and runs the real vendored `matcher.score_job` through the mocked proxy to a result. Suite 275 green.

## Phase 3: App auth

- [ ] 3.1 Sign up, sign in, email verification, password reset via Supabase. **Acceptance:** each flow works against the Supabase project; UI + unit tests (mocked Supabase).
- [ ] 3.2 Refresh token in OS keychain only; silent session refresh. **Acceptance:** refresh token stored via keyring (never plaintext on disk); access token refreshed silently before expiry; test asserts keychain-only storage.
- [ ] 3.3 Sign out + account deletion. **Acceptance:** sign-out clears keychain+session; account deletion calls Supabase admin delete and wipes local data; tests.
- [ ] 3.4 Entitlement-lapsed UI state: app opens, explains, links to Stripe customer portal, bot will not start. **Acceptance:** with an inactive entitlement, the app shows the lapsed screen + portal link and preflight blocks start (ENTITLEMENT_INACTIVE).

## Phase 4: Onboarding wizard (resumable, ordered)

- [ ] 4.1 Step 1 Account + trial/subscription activation. **Acceptance:** new user signs up + activates a trial/subscription (Stripe test); state persisted; resumable.
- [ ] 4.2 Step 2 Environment check: Chrome/bundled browser, disk writable, proxy reachable. **Acceptance:** each check reports pass/fail with a fix action; blocks advance on fail.
- [ ] 4.3 Step 3 Profile: personal details, work rights, notice period; resume upload with parse-and-prefill. **Acceptance:** resume parsed, fields pre-filled for user confirmation (not typed); saved to config.
- [ ] 4.4 Step 4 Screening answer bank (work rights, licence, notice period, salary expectation, years experience, relocation). Unknown-question policy: skip the job + flag; never improvise. **Acceptance:** answer bank persisted; a job with a question not in the bank is skipped+flagged (test); bot never improvises.
- [ ] 4.5 Step 5 Job criteria: titles, keywords, locations, salary range, work type, score threshold, daily cap. **Acceptance:** criteria saved to config and honored by scrape+apply.
- [ ] 4.6 Step 6 Seek connect: user logs in themselves in a controlled browser window; session captured + encrypted locally; password never seen. **Acceptance:** session captured into encrypted local store; verify probe returns VALID; no password handled by us.
- [ ] 4.7 Step 7 First-run review mode: first batch requires per-application approval; autonomous unlocks after the user approves from review mode. **Acceptance:** first batch forces review; autonomous mode flag flips only after an approval; review-mode honors the score threshold (invariant).
- [ ] 4.8 Wizard is resumable mid-way and strictly ordered. **Acceptance:** quitting mid-wizard resumes at the same step; steps cannot be skipped out of order; test.

## Phase 5: Preflight gate

- [x] 5.1 Hard checklist on EVERY bot start (not just after onboarding), coded errors each with a fix action deep-linking to the right wizard step: MISSING_RESUME, INCOMPLETE_ANSWER_BANK (lists missing questions), NO_CRITERIA, SEEK_SESSION_EXPIRED, ENTITLEMENT_INACTIVE, BROWSER_MISSING, PROXY_UNREACHABLE, KILL_SWITCH_ACTIVE. **Acceptance:** each red condition produces its coded error + deep-link; unit test per code. **DONE** (app commit b274e0f): `engine/preflight.py` with all 8 `PreflightCode`s, `WIZARD_STEP` deep-links, the 6 required answer-bank fields (from seek_apply), `run_preflight()` + `assert_ready()` (raises `PreflightBlocked` with every red check; INCOMPLETE_ANSWER_BANK lists exact missing fields). 15 tests; suite 290 green.
- [~] 5.2 Engine cannot start with any check red; no bypass flag. **Acceptance:** a red preflight blocks engine start on every entry path (single run, batch, scrape-and-apply); test asserts no bypass exists (invariant). **PARTIAL** (in b274e0f): the gate PRIMITIVE is built + tested — `assert_ready()` raises `PreflightBlocked` and has no bypass/force/skip parameter (invariant test passes). Wiring it into the worker entry runners (`_apply_runner`/`_batch_*`/`_scrape_runner`) is SEQUENCED WITH Phase 3/4: it needs the live-check plumbing (entitlement via proxy token, browser probe, proxy reachability, kill switch) AND onboarding-complete test fixtures, else a half-wired gate trips INCOMPLETE_ANSWER_BANK on the existing 248 (their fixtures predate the answer bank). Do the worker wiring after 3.x/4.x land.

## Phase 6: Application management UI

- [ ] 6.1 Dashboard: today's activity, queue depth, quota remaining. **Acceptance:** dashboard reads live state; quota from proxy usage.
- [ ] 6.2 Queue with states pending/approved/applied/skipped/failed; per-job detail: score+rationale, generated docs preview, exact answers to be submitted. **Acceptance:** queue renders states; detail shows score rationale + doc preview + the literal screening answers.
- [ ] 6.3 Review mode approve/reject; company blocklist checked before submit (must be able to exclude current employer). **Acceptance:** approve/reject works; a blocklisted company is never submitted (test); blocklist editable.
- [ ] 6.4 Application history, searchable, CSV export. **Acceptance:** history searchable; CSV export matches jobs.db records.
- [ ] 6.5 Pause/resume + stop control that halts within one job. **Acceptance:** stop halts before the next job starts and cancels gracefully; pause/resume works; test.
- [ ] 6.6 Notifications: application sent, run finished, failure, session expired. **Acceptance:** each event fires a notification; test per event.

## Phase 7: Resilience and ops

- [ ] 7.1 Seek session expiry detection + re-login prompt. **Acceptance:** expiry detected, run pauses, re-login prompt shown; test (mocked expiry).
- [ ] 7.2 Captcha/anti-bot detection pauses the run + notifies; never retries through it. **Acceptance:** detection halts the run, notifies, and does NOT retry; test.
- [ ] 7.3 Crash recovery: submit records idempotent; restart never double-applies. **Acceptance:** an interrupted submit is reconciled on restart and never re-submitted (extends existing recover_orphans + same-role dedup invariants).
- [ ] 7.4 Sentry crash reporting. **Acceptance:** an unhandled exception reports to Sentry (test DSN); PII-scrubbed.
- [ ] 7.5 PostHog events routed server-side via the proxy; app sends version header on every proxy call. **Acceptance:** events reach PostHog through the proxy; every proxy call carries the client version header.

## Phase 8: Packaging

- [ ] 8.1 Bundle Chromium via Playwright. **Acceptance:** packaged app launches a browser with nothing pre-installed (Chromium bundled, not fetched at runtime).
- [ ] 8.2 Remove LibreOffice + claude CLI dependencies entirely; replace any conversion they performed. **Acceptance:** no soffice/claude CLI dependency remains; docx->pdf (or equivalent) works without LibreOffice; test.
- [ ] 8.3 Auto-update mechanism. **Acceptance:** the app checks for + stages an update; structured so signing slots in later.
- [ ] 8.4 Unsigned build now; signing/notarization BLOCKED on Apple Developer cert; build structured so signing slots in without rework. **Acceptance:** unsigned build produced; signing hook present but no-op; documented BLOCKED.
- [ ] 8.5 Fresh install on a clean machine profile completes onboarding and reaches a green preflight with nothing pre-installed except the OS. **Acceptance:** clean-profile install -> onboarding -> green preflight, end to end.

## Phase 9: Final validation

- [ ] 9.1 Full regression suite green (incl. all prior invariant tests). **Acceptance:** `pytest` (non-live) green, >= 248 + new tests.
- [~] 9.2 Live re-validation pilot through the NEW API path: 5 jobs from criteria, autonomous, all prior per-submission checks (confirmation element, Applied badge, confirmation email, SQLite record, reconciled counts), halt on any failure, hard cap 5, throttle floor active. **Acceptance:** 5/5 verified via the proxy LLM path; halt-on-failure honored. **DRY-RUN VERIFIED 2026-06-15**: full slice (peek->score->tailor->drive Seek form->review&submit) ran on a REAL Seek job (92690665) entirely through the DEPLOYED proxy `https://api.autoapply.com.au` (ProxyLLM routed score+tailor+screening; salary answered via Claude->proxy AUD $145k); SafetyGate stopped at submit-ready, 0 submissions, status=dry_run_verified. Harness `/tmp/pilot_dryrun.py` (token provider + AUTOAPPLY_PROXY_URL=prod, pilot workdir ~/autoapply-pilot/engine). REMAINING: the 5-job REAL autonomous submit (allow_real_submit=True) — AWAITS EXPLICIT USER GO-AHEAD (outward-facing: real applications to real employers under the user's Seek profile).
- [ ] 9.3 Smoke checklist run twice on the packaged build. **Acceptance:** both runs pass.
- [ ] 9.4 Update HANDOFF.md: changes, test evidence, pilot results, decisions, blocked items, exact manual-test script. **Acceptance:** HANDOFF.md current.

---

## Recon summary (2026-06-12)

The proxy (`autoapply.com.au/proxy-server`, FastAPI) already implements most of
Phase 1: task-scoped LLM endpoints (`/api/llm/score-job|tailor-resume|cover-letter|screening|screening-one`)
with proxy-owned prompts + model routing + max-token caps, Supabase-JWKS JWT
validation, the `subscriptions` entitlements table + Stripe webhook writers
(4 events), 60s subscription cache, per-user rate limit + free-tier application
quota, and usage metering to Postgres. It is NOT deployed (no EC2; api.autoapply.com.au
unreachable). Stripe TEST infra is live on the shared `Quantumloop ai` account
(acct_1TKsOzBRNYIeyxhv): 4-tier products + prices + an enabled webhook endpoint.
`seekautoapply` has liftable code (preflight engine, Fernet/AES session
encryption, entitlement/usage schema, cost-cap logic). The autoapply-next client
has NO auth layer (signin is a stub), a 2-check log-only preflight, and the
engine still shells to the claude CLI via the single chokepoint
`vendor/job-finder/claude_cli.py:claude_complete`.

## Blocked items (escalated; see HANDOFF.md)

- **[B] Supabase project DELETED.** `ndkeryoqlvktzuzxubvb.supabase.co` returns
  NXDOMAIN; no SERVICE_ROLE_KEY or JWT_SECRET exists anywhere. Blocks LIVE auth +
  entitlements integration (1.2/1.3/1.4 live verification, 3.x live, 4.1 live)
  and the pilot. Code is buildable against mocks; live needs a reprovisioned
  project + service-role key + JWT secret from the operator.
- **[B] No Anthropic/OpenAI API key.** Migrating off the claude CLI to the API
  needs a real ANTHROPIC_API_KEY (+ OPENAI_API_KEY for scoring) = per-token
  billing. None in env/SSM/.env. Blocks 1.9 (real staging call), 2.3/2.4 live
  quality parity, and 9.2 (live pilot via API path). Operator must supply the
  key(s); I will store them in AWS SSM.
- **[B] Proxy deploy** is gated on the two above (deploying an empty-secret proxy
  is pointless). I have AWS Admin access + orphaned SG/key-pair + Terraform ready,
  so I can deploy as soon as the Supabase + Anthropic secrets exist.
- **[B] 8.4 signing/notarization** BLOCKED on Apple Developer cert (user decision).

## Decisions log (made on the user's behalf this sprint; mirrored to HANDOFF.md)

- **D1 Endpoint contract:** add a generic proxy `/api/llm/complete` endpoint
  (vendor prompts sent verbatim) + a `claude_complete`-shaped client shim, rather
  than reshaping the 5 task endpoints. Lowest-risk seam; preserves all 5 engine
  call-site contracts (matcher 0-100 score, resume JSON, cover-letter text+quality
  gate, salary JSON, screening text) with zero shape translation. The 5 task
  endpoints stay for the Electron client.
- **D2 Secrets store:** AWS SSM Parameter Store SecureString in ap-southeast-2
  (simplest, cheapest; I have write access). Not Secrets Manager.
- **D3 Region:** ap-southeast-2 (all existing proxy artifacts + DynamoDB there).
- **D4 Subscription cache TTL:** align to the blueprint's 5 min (300s), made
  configurable; note 60s was the safer original.
- **D5 App auth identity:** email + password (Phase 3 spec says email verify +
  password reset), replacing the phone-based stub.
- **D6 Entitlement source of truth:** the client checks entitlement via the proxy
  (Supabase-token-authed), never Stripe directly (no client Stripe SDK).
- **D7 soffice replacement:** render the resume PDF with reportlab/Platypus (as
  the cover letter already is), dropping LibreOffice; no vendor edit (monkey-patch
  tailorer's converter at the adapter layer).
- **D8 Chromium:** bundle via Playwright in the spec (8.1 acceptance requires it).
- **D9 EC2 auth:** the production instance uses a least-privilege instance role
  (ssm:GetParameter + kms:Decrypt + sns:Publish), not the Admin user's static keys.

## Phase 1 status from recon
- 1.1 task endpoints: DONE (exist); add `/api/llm/complete` per D1. 1.2 JWKS: code DONE, live BLOCKED. 1.3 entitlements+webhooks: code DONE, live BLOCKED. 1.4 cache: change 60s->300s (D4). 1.5 rate limit DONE; daily token quota MISSING. 1.6 metering: DONE. 1.7 remote config: MISSING. 1.8 SSM loader: MISSING (key value BLOCKED, loader buildable+live-testable). 1.9 real staging call: BLOCKED.

## AUDIT 2026-06-15 (7-agent read-only ground truth; supersedes 06-12 recon where they differ)
- **Phase 1:** 1.1/1.4/1.5/1.6 NOW DONE+committed this session (proxy suite 49 green); 1.2/1.3 code-done + live-BLOCKED; 1.9 BLOCKED.
- **Phase 2 (engine->proxy): ALL MISSING.** 2.1 adapter (mirror `autoapply.com.au/backend/llm/proxy_client.py`; route matcher.score_job + tailorer.tailor + seek_apply answer handlers through the proxy `/api/llm/complete` via a monkey-patch installer like `src/autoapply_next/engine/hooks.py`) UNBLOCKED for code+mock. 2.2 failure handling (401 refresh-retry-once, kill-switch) UNBLOCKED. 2.3 golden parity BLOCKED(keys). 2.4 engine-runs-without-claude test UNBLOCKED.
- **Phase 3 (auth): ALL MISSING** (signin is a stub, no keyring/Supabase client). UI + mocked-Supabase unit tests + keyring(3.2) buildable now; live round-trip BLOCKED on Supabase keys.
- **Phase 4 (wizard): MISSING as a coordinated wizard;** 4.3/4.5/4.6 PARTIAL pieces exist (profile, single-keyword scrape, plaintext Seek session). 4.2/4.3/4.4/4.5/4.6/4.7/4.8 UNBLOCKED; 4.1 BLOCKED(auth/Stripe).
- **Phase 5 (preflight): MISSING** (only a 2-check log-only `preflight_report`). 5.1 (8 coded errors + wizard deep-links) + 5.2 (hard gate every entry path in worker.py, no bypass) fully UNBLOCKED, high value.
- **Phase 6 (mgmt UI): PARTIAL.** 6.1 dashboard MISSING; 6.3 review+blocklist MISSING; 6.6 notifications MISSING; 6.2 queue/detail PARTIAL; 6.4 history PARTIAL (need search+CSV); 6.5 PARTIAL (STOP done; need pause/resume). All UNBLOCKED.
- **Phase 7 (resilience):** 7.1/7.2/7.3 DONE+tested. 7.4 Sentry MISSING (buildable w/ test DSN). 7.5 PostHog+X-Client-Version MISSING (version header rides on the 2.1 client).
- **Phase 8 (packaging):** 8.1 Chromium + 8.4 unsigned+signing-hooks DONE. 8.2 remove soffice/claude MISSING (D7 reportlab resume PDF + adapter monkey-patch; cover-letter already uses reportlab) UNBLOCKED. 8.3 auto-update MISSING (tufup declared but stubbed) UNBLOCKED. 8.5 clean-install PARTIAL (blocked by 8.2 + Phase 4).
- **Unblocked grind order (top-to-bottom):** 2.1 -> 2.2 -> 2.4 -> 5.1 -> 5.2 -> 4.2/4.3/4.4/4.5/4.6/4.7/4.8 -> 6.1/6.3/6.4/6.5/6.6 -> 8.2 -> 8.3 -> 3.2 -> 7.4 -> 7.5(partial).
- **Live-blocked (await Supabase MCP auth + Anthropic/OpenAI keys):** 1.2/1.3 live, 1.9, 2.3, 3.1/3.3/3.4 live, 4.1, 9.2 pilot. Schema ready at `autoapply.com.au/proxy-server/supabase/schema.sql`.

## LIVE CREDS / INFRA STATUS 2026-06-15 (blockers cleared via Supabase MCP + Secrets Manager)
- **Supabase project VERIFIED LIVE** (the 06-12 "DELETED/NXDOMAIN" claim was wrong): `ndkeryoqlvktzuzxubvb` name `autoapply`, org `vkoujxmhnywxzdbghsxl`, ap-southeast-2, ACTIVE_HEALTHY, PG17. **Schema already fully applied** (migration `autoapply_initial_schema`): subscriptions/usage_log/phone_verifications + all columns + 3 RPCs + on_auth_user_created trigger + 2 RLS policies all verified match `schema.sql` -> NO re-apply needed.
- **JWT signing is ASYMMETRIC** (JWKS returns an ES256 key, reachable at `/auth/v1/.well-known/jwks.json`). So the proxy needs NO `SUPABASE_JWT_SECRET` (auth.py JWKS path handles it); 1.2's prerequisite is live-verified.
- **SSM stored** (ap-southeast-2): `/autoapply/prod/ANTHROPIC_API_KEY` (SecureString, 108-char sk-ant key from Secrets Manager `API_key_AWS_SSM`), `/autoapply/prod/SUPABASE_URL` (`https://ndkeryoqlvktzuzxubvb.supabase.co`).
- **STILL NEEDED for live (from dashboard):** `SUPABASE_SERVICE_ROLE_KEY` (proxy entitlement/usage writes -> 1.3 live + pilot). Then: deploy proxy (EC2+Caddy, AUTOAPPLY_SECRETS_SSM_PREFIX=/autoapply/prod/) -> live-verify 1.2/1.3/1.9 -> Phase 9 pilot. OPENAI_API_KEY only needed for the Electron client's /score-job (engine scores via Anthropic /complete, so not on the pilot path).
- **Supabase MCP access:** `plugin:supabase:supabase` (OAuth'd this session). The claude.ai desktop Supabase connector is NOT bridged to the CLI session (no callable tools).

## EC2 PROXY DEPLOYED 2026-06-15 (Phase 1 infra — live)
- **Instance** `i-0f88964bf5c0985cd`, t3.small, ap-southeast-2, public IP **3.26.2.30**, Ubuntu 24.04. Reuses orphaned SG `autoapply-proxy-sg` (80/22/443), key `autoapply-proxy-key`, instance profile `autoapply-proxy-profile`. (Did NOT use terraform — its main.tf would collide with the existing SG/role; launched via `run-instances`.)
- **IAM:** added inline policy `SsmReadAutoapplyProd` to `autoapply-proxy-role` (ssm:GetParameter/GetParametersByPath on `/autoapply/prod/*` + kms:Decrypt) per D9, so the instance reads secrets from SSM with no static keys.
- **Proxy:** `/opt/autoapply` (scp'd proxy-server), venv, systemd unit `autoapply-proxy` (Restart=always, `AUTOAPPLY_SECRETS_SSM_PREFIX=/autoapply/prod/`, region ap-southeast-2). On-box `GET /api/config` -> 200 (proves SSM load via instance role). Caddy reverse_proxy api.autoapply.com.au -> 127.0.0.1:8080 (active; returns 308 HTTP->HTTPS).
- **DNS + TLS DONE:** Porkbun A record `api` -> `3.26.2.30` created (id 555938080, via Porkbun API). `https://api.autoapply.com.au` LIVE: `/api/config` -> 200; real Supabase JWT -> `/api/llm/complete` -> real Anthropic `{"text":"deployed"}`; valid Let's Encrypt cert (Jun15->Sep13 2026). Phase 1 EC2+Caddy infra COMPLETE; 1.9 `/complete` verified on prod.
- **Pilot can run NOW against the LOCAL proxy** (`http://127.0.0.1:8080`, verified) without waiting on DNS. Real Seek submits require explicit go-ahead (outward-facing).
- Test Supabase user for live checks: `sagarvd130+proxytest@gmail.com` (id c6c25041-...), free/active, in prod auth.

## ENGINE HARDENING 2026-06-15 (deferred items closed before exposing the engine; NO real submit, allow_real_submit stayed False)
Investigation: 3-agent read-only sweep (throttle/verifier/dedup) with file:line evidence.
- **[x] Throttle floor — already enforced on every path incl. chained; CONFIRMED, no change.** Floor `APPLY_GAP_MIN=60` clamped at the `run_batch` chokepoint (`batch.py:482`); chained `_scrape_then_apply_runner` routes BOTH Phase 0 (`worker.py:605`) and Phase 2 (`worker.py:723`) through it. Invariant test already exists: `test_throttle_proof.py:48` `test_scrape_and_auto_apply_floors_throttle_on_live_both_phases` (asserts lo>=60 on both chained phases with throttle OFF + LIVE). 7 throttle tests green.
- **[x] Verifier false-positive — FIXED** (commit 292136d): whole-page job-id scan no longer reports APPLIED alone (it was matching recommended-rail ids; applied cards carry no /job link). Bare id -> UNCERTAIN -> SUBMITTED_UNCERTAIN unless corroborated by the card title+company match. Suite 291 green.
- **[x] Same-role dedup — within-run invariant test ADDED** (commit 6554581): already sound (sequential run_batch + in_progress early-write); added `test_same_role_blocked_within_run_when_sibling_enters_apply` proving the real `persist_in_progress(job #1)` flips a same-role job #2 from eligible to blocked within one run. No bug found; this is a regression guard. Suite 292 green.

### ROUND 2 — operator decisions resolved + applied (2026-06-15)
- **#1 jitter + volume cap — read-only confirm DONE.** Jitter EXISTS (`batch.py:634` `random.uniform`); per-run hard cap EXISTS (`MAX_APPLIES_PER_RUN=100`, `worker.py:73`); daily_cap wired from UI (`queue_screen.py:221`, default 30) + enforced (`batch.py:607-628`). **SURFACED (not implemented, cap value is operator's):** the daily cap is effectively PER-RUN — `today_count_fn` is never wired, so it counts only the current run's submissions; multiple runs/day each reset. `settings_screen.py:174` falsely says "not yet enforced" (stale). Decision: set a true daily-cap value + wire `today_count_fn` (count today's `applied` rows)?
- **#2 company stop-list — DONE** (commit d6e00bf): strip corporate filler (`_COMPANY_FILLER_TOKENS`) then require >=1 meaningful token. Fixes filler-collision false-positive AND cross-suffix false-negative. **SURFACED:** ambiguous tokens EXCLUDED (tech/global/international/holdings/partners/digital) — add any? Edge: two employers sharing one meaningful token + same title can match (the >=1-token rule you specified).
- **#3 dedup suffix stripping — NO-OP** (kept strict, per your call).
- **#4 empty-metadata bypass — DONE** (commit bc5ab78): `dedup_before_apply` never silently never-blocks — role metadata -> same-role; missing -> Seek job-id listing dedup; missing + no id -> `UndedupableListingError` -> 'skipped' (re-queue to confirm). **SURFACED:** job-id regex `_SEEK_JOB_RE = ^https?://.../job/(\d+)` is the one place to revisit if Seek URLs change.
