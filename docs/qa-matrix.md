# QA Matrix: every interactive control

Date: 2026-05-30. Maintained alongside `tests/ui_tests/test_interaction_audit.py`
and `tests/ui_tests/test_empty_state.py`, which exercise each row below.

**Legend**
- **WORKS** — connected, handler does real work, observable effect verified
  by a test that fails if the control becomes silent.
- **FIXED** — was silent or partially broken before today's pass; the row
  notes the fix and the test that protects against regression.
- **DISABLED-DEFERRED** — feature is on the roadmap. Control is explicitly
  disabled with a tooltip explaining why; clicking it cannot do nothing
  silently because the user cannot click it.

## MainWindow

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| Sign in nav action | QAction | enabled | WORKS | `_goto(self._signin)`; Ctrl+1. |
| Seek session nav action | QAction | enabled | WORKS | `_goto(self._session)`; Ctrl+2. |
| Profile nav action | QAction | enabled | WORKS | `_goto(self._profile)`; Ctrl+3. |
| Queue nav action | QAction | enabled | WORKS | `_goto(self._queue)`; Ctrl+4. |
| Run nav action | QAction | enabled | WORKS | `_goto(self._run)`; Ctrl+5. |
| Results nav action | QAction | enabled | WORKS | `_goto(self._results)`; Ctrl+6. |
| Settings nav action | QAction | enabled | WORKS | `_goto(self._settings_screen)`; Ctrl+7. |
| Status-bar DRY-RUN / LIVE SUBMIT badge | QLabel | n/a (display) | WORKS | `_on_allow_real_submit_changed` updates colour + text. |

## SignInScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| Phone field | QLineEdit | disabled | DISABLED-DEFERRED | Supabase wiring is roadmap. Tooltip says so. |
| Password field | QLineEdit | disabled | DISABLED-DEFERRED | Same. |
| "Sign in" button | QPushButton | disabled | DISABLED-DEFERRED | Same. Cannot do nothing because not clickable. |
| "Skip (dev mode)" button | QPushButton | enabled | **FIXED** | Was the dead button: it emitted `authenticated("dev-user")` but nothing was connected, so clicking did literally nothing. Now MainWindow listens; click routes to the Seek session screen and shows a status-bar confirmation. Test: `test_signin_skip_button_emits_authenticated`. |

## SessionSetupScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| Status banner | QLabel | n/a | WORKS | Colour-coded per state (`valid/invalid/abandoned/cancelled/error/missing`). |
| "Open Seek to log in" | QPushButton | enabled when idle | WORKS | Calls `worker.launch_session_browser()`. Disabled while busy. Test: `test_session_launch_drives_worker`. |
| "Cancel" | QPushButton | enabled only when running | WORKS | Calls `worker.cancel()`. State-coupled enable. |
| "Re-check existing session" | QPushButton | enabled | WORKS | Re-reads user-data-dir, updates banner. Test: `test_session_re_check_changes_status`. |

## ProfileScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| Name | QLineEdit | enabled | WORKS | Bound to config.yaml on save. |
| Email | QLineEdit | enabled | WORKS | Validated for `@` on save. |
| Phone | QLineEdit | enabled | WORKS | Bound to config.yaml on save. |
| Resume profile text | QTextEdit | enabled | WORKS | Bound to assets/profile.txt on save. |
| "Save profile" | QPushButton | enabled | **FIXED** | Previously: silently wrote empty values, no validation. Now validates name/email/phone/resume presence and shows a dialog listing what is missing; writes atomically via `<file>.tmp` then `replace`. Tests: `test_profile_save_empty_shows_error`, `test_profile_save_writes_atomically`. |

## QueueScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| Keyword input | QLineEdit | enabled when idle | WORKS | Tracks `settings.last_scrape_keyword`; Enter triggers scrape. |
| "Scrape and score" | QPushButton | enabled when idle | **FIXED** | Empty-keyword check used to update a small status label; now opens a dialog and focuses the input. Test: `test_queue_scrape_empty_keyword_shows_dialog`. |
| "Cancel" | QPushButton | enabled only when running | WORKS | Calls `worker.cancel()`. |
| "Only queued" checkbox | QCheckBox | enabled | WORKS | Filters table. |
| "Only score >= threshold" checkbox | QCheckBox | enabled | WORKS | Threshold value from Settings; label re-renders when threshold changes. |
| "Reload table" | QPushButton | enabled | WORKS | Re-reads jobs.db. Test: `test_queue_reload_table_button_runs`. |
| Per-row "Run dry-run" | QPushButton | enabled when idle | **FIXED** | Previously stayed enabled while the worker was busy, leading to a confusing "engine busy" error bounce after a click. Now `_on_worker_state` disables every row's button while running, with a tooltip. |
| Scrape progress | QProgressBar | visible only when running | WORKS | Indeterminate; couples to `_on_worker_state`. |

## RunScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| URL field | QLineEdit | enabled | WORKS | Preloaded by `set_url(url)` when Queue routes a job in. |
| "Run dry-run" / "Run with LIVE submit" | QPushButton | enabled when idle | WORKS | Label and red/green styling flip on `allow_real_submit_changed`. Validates URL (must contain `seek.com`) and pops dialogs for empty / non-Seek URLs. Tests: `test_run_empty_url_shows_dialog`, `test_run_non_seek_url_shows_dialog`. |
| "Cancel" | QPushButton | enabled only when running | WORKS | Calls `worker.cancel()`. Test: `test_run_cancel_button_calls_worker_cancel`. |
| Stage strip | 5 QLabels | n/a (display) | **FIXED** | On FAILED the previously-active stage now turns red instead of staying blue. `_mark_failed_stage` recolours. |
| Live log | QPlainTextEdit | read-only | WORKS | Auto-scrolls. |
| Screenshot pane | QLabel | n/a (display) | **FIXED** | Previously stuck at "(running...)" on FAILED, the visible regression that prompted this whole pass. Now FAILED, SKIPPED_LOW_SCORE, SUBMITTED, and CANCELLED each replace it with a coloured panel; FAILED also opens an error dialog with a friendly explanation per known exception type. Test: `test_run_failed_status_shows_error_panel_and_dialog`. |

## ResultsScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| "Refresh" | QPushButton | enabled | WORKS | Re-reads jobs.db. Test: `test_results_refresh_runs_without_db`. |
| History table | QTableWidget | enabled | WORKS | Row selection drives detail. Test: `test_results_select_row_with_no_db_does_not_crash` covers the empty case. |
| Detail tabs (cover letter / Q&A / raw) | QTabWidget | enabled | WORKS | Read-only panes. Cover letter reads `<cover_pdf>.txt`; Q&A reads `errors/applications.jsonl`. |

## SettingsScreen

| Control | Type | State | Verdict | Notes |
|---|---|---|---|---|
| "I understand. Allow real submission." | QCheckBox | enabled | WORKS | On->off does not prompt; off->on triggers a confirmation dialog (`QMessageBox.question`); cancel rolls the check back. Tests: `test_settings_real_submit_cancel_does_not_flip`, `test_settings_real_submit_confirm_flips_and_back_off_without_prompt`. |
| LIVE SUBMIT state badge | QLabel | n/a | WORKS | Re-rendered by `_refresh_state_label`. |
| Match threshold | QSpinBox | enabled | WORKS | Writes through to SettingsStore; emits `match_threshold_changed` so Queue and MainWindow react. Test: `test_settings_threshold_changes_setting`. |
| Daily cap | QSpinBox | enabled | **FIXED** | Was wired but lied about being enforced. Now has a tooltip + footnote stating "stored but not enforced" so the user knows changing it does nothing yet. |

## Global error safety net

| Hook | What it catches | Where it surfaces | Test |
|---|---|---|---|
| `sys.excepthook` | Main-thread uncaught Python exceptions | `ErrorBus.error` → MainWindow non-blocking dialog | `test_excepthook_emits_bus_and_logs` |
| `qInstallMessageHandler` | Qt warnings (log only) and criticals/fatals (dialog) | Same bus | `test_qt_message_handler_routes_critical_to_bus` |
| `@safe_slot` | Exceptions raised inside any wrapped Qt slot | Same bus | `test_safe_slot_catches_exception_and_emits_bus`, `test_safe_slot_decorator_on_qobject_method` |
| `show_error_dialog` | The single way the GUI builds an error popup | n/a (the surface itself) | `test_show_error_dialog_creates_visible_messagebox` |
| `EngineWorker._{apply,session,scrape}_runner` | Any exception inside the worker's async runners | `worker.failed.emit(op, msg)` and `MainWindow._on_worker_failed` | Existing worker tests (4) and the new audit tests |

## What is still rough (no test can catch these, the user must)

- **Long apply runs (3 to 5 minutes) have no per-stage time hint.** The progress
  bar is indeterminate. Watching the live log is fine; first-time users may
  wonder if it has stalled.
- **The screenshot pane on FAILED resets its border on the next click.** It
  does not currently auto-reset on a fresh Run click; we deliberately keep
  the failure visible until the next run overwrites it. If the user keeps
  the failure on screen and clicks back to it after some other action, the
  red panel is still there.
- **Cover-letter and Q&A previews assume the .txt sidecar exists.** Older
  runs from before the adapter wrote sidecars show "(no sidecar found)".
  Real, not a bug.
- **The Settings 'daily cap' control has no enforcement.** Documented in
  its tooltip but a user might still set it and expect behaviour.
- **The Sign-in stub button leaves the toolbar action checked on Sign In
  even after MainWindow routes to Seek session.** Cosmetic.
- **High-DPI / dark-mode rendering on macOS is browser-default Qt.** Not
  Aqua-native. A non-technical tester will notice the controls are flat
  instead of Aqua-styled. Out of scope.
