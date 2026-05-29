# ADR-0002: Desktop framework decision

Date: 2026-05-29
Status: Proposed (awaiting user sign-off; Phase 1 checkpoint)

## Context

The engine is Python (`job-finder/`). Per ADR-0001 it is unmodified and exposes its application path as a set of async Python functions. The single most consequential axis of this decision is whether the GUI runs **in-process** with the engine (no IPC, the GUI imports the engine directly) or runs the engine as a **sidecar** behind an HTTP/IPC boundary. The old Electron product chose sidecar and the artefacts of that choice are visible in its codebase: a temp-file port handshake, a runtime-generated bearer token, a health-check loop, graceful SIGTERM with a 5 s grace window, a separate "backend crashed" error window, and six documented divergences in `debug-log.md` between the in-tree Seek automation and `job-finder`. None of that is incompetence; it is the standard tax of having a process boundary between the GUI and a Python engine.

The user's prompt explicitly weights this: "a Python-native GUI runs the engine IN-PROCESS with no IPC, eliminating the whole class of sidecar bugs that plague webview+sidecar apps."

## Options evaluated

### A. PySide6 (Qt for Python) + PyInstaller, in-process

A Python desktop GUI that imports `job-finder` directly. The engine is called as ordinary Python via Qt's `asyncio` integration (`qasync` or a `QThreadPool` worker). No IPC, no port discovery, no auth handshake, no second process to monitor. Cross-platform via Qt's native renderers on macOS and Windows. Packaging via PyInstaller; code-signing via Apple `codesign` + `notarytool` (macOS) and `signtool` (Windows). Auto-update is the area of relative weakness: PyUpdater or a bespoke "check feed, download installer, restart" is required (no electron-updater equivalent that's plug-and-play).

### B. Tauri (Rust shell + system webview) + Python sidecar

Modern webview architecture. Renderer is HTML/JS, can in principle reuse the old React + Zustand + Tailwind code. Rust is the bridge. Python `job-finder` runs as a sidecar via Tauri's `Command` API. Bundle is small (around 10 to 20 MB shell). Code-signing is supported on both platforms (`tauri build` integrates), auto-update is built in. **But the Python sidecar boundary is back.** Port discovery, bearer token, health check, lifecycle management, crash detection: all of it must be implemented again. Rust adds a second language to maintain.

### C. Electron + React + Python sidecar (status quo)

The old product's choice. We could resume from the existing renderer, main, preload, and tray. The signing and update story is the most mature of any option (`electron-builder` + `electron-updater`). The sidecar pain is unchanged from option B and is on display in the old codebase. Bundle is the largest (about 150 MB).

### D. Wails (Go shell + system webview) + Python sidecar

Same shape as Tauri but with Go instead of Rust. Go is a marginally easier language than Rust for a small team. No clear technical advantage over Tauri; smaller community; an additional language nobody on this project is using elsewhere. Dismissed quickly.

## Weighted comparison

Weights reflect what actually matters for *this* project: a Python engine with a known, finite product surface, two contributors at most, principal-bar engineering ("simplest design that meets the requirement, evidence over opinion"). Integration risk gets the heaviest weight because the old product's pain is evidence that risk is real, not theoretical. Scores are on 1 (worst) to 5 (best). Totals are arithmetic, double-checked.

| Criterion | Weight | PySide6 | Tauri | Electron | Wails |
|---|---:|---:|---:|---:|---:|
| Integration friction with Python engine (no sidecar = best) | 30 | 5 | 2 | 2 | 2 |
| Code-signing + notarization maturity on macOS and Windows | 15 | 3 | 4 | 5 | 3 |
| Auto-update maturity | 10 | 2 | 4 | 5 | 3 |
| Bundle size and runtime footprint | 5 | 4 | 5 | 2 | 5 |
| Dev velocity from our current position | 15 | 3 | 3 | 5 | 2 |
| Security and PII posture (fewer boundaries to mishandle secrets across) | 10 | 5 | 4 | 3 | 4 |
| Long-term maintainability (fewer moving parts) | 10 | 5 | 3 | 2 | 3 |
| Ecosystem of UI components and accessibility primitives | 5 | 4 | 5 | 5 | 4 |
| **Weighted total (max 5.00)** | | **4.00** | **3.25** | **3.45** | **2.80** |

Worked totals (sum of weight times score, divided by 100):

- PySide6: (150 + 45 + 20 + 20 + 45 + 50 + 50 + 20) / 100 = **4.00**
- Tauri: (60 + 60 + 40 + 25 + 45 + 40 + 30 + 25) / 100 = **3.25**
- Electron: (60 + 75 + 50 + 10 + 75 + 30 + 20 + 25) / 100 = **3.45**
- Wails: (60 + 45 + 30 + 25 + 30 + 40 + 30 + 20) / 100 = **2.80**

Notable head-to-heads:

- **PySide6 vs everything else on integration (5 vs 2).** Engine is Python. In-process means a function call. Sidecar means a port handshake, a bearer-token negotiation, a health check, lifecycle management, crash detection, and a separate Python automation that turned out to diverge from `job-finder` in six documented ways. PySide6 erases all of it. This single 30-weight criterion is what makes PySide6 win by 0.55 over Electron.
- **PySide6 vs Electron on signing and updates (3 vs 5, 2 vs 5).** PyInstaller + `codesign` + `notarytool` + `signtool` work and are documented, but they are not the one-command `electron-builder` + `electron-updater` story. The auto-updater for a PySide6 app is a few hundred lines of bespoke Python (poll GitHub releases, verify signature, download, swap binary on restart). This is the explicit cost of the in-process win.
- **PySide6 vs Tauri / Electron on UI dev velocity (3 vs 3 vs 5).** Electron is the only option where the existing React renderer and the existing main / preload / tray code both carry forward verbatim. Tauri lets the renderer carry but rewrites the shell in Rust. PySide6 rebuilds the UI in Qt Widgets or QML. The minimal product is seven screens, none visually exotic; the audit calls the old renderer "professional but minimal." Rebuild cost is a few days, not weeks.
- **Electron vs Tauri (3.45 vs 3.25).** Electron edges Tauri only because we can resume from existing code. If we were greenfield with no prior shell, Tauri's scores on bundle size, security posture, and maintainability would flip the ranking. **The two are close enough that this is a real choice, not a math accident.**

## Decision (proposed)

**Adopt PySide6 (Qt for Python), build the GUI in-process with the engine, package with PyInstaller, code-sign with Apple `codesign` + `notarytool` on macOS and `signtool` on Windows, ship a minimal GitHub-releases-based updater.** Vendor `job-finder/` as an unmodified, pinned subtree. The engine adapter is a thin Python module that exposes `apply_to_job`, `score_job`, `tailor`, an async progress callback, and the `ALLOW_REAL_SUBMIT` gate.

The carry-forward from the old product becomes: Supabase auth flow (ported to Python, not React; `supabase-py` exists and the JWT logic is straightforward), database schema + migrations (SQL is portable), the OS keychain wrapper pattern, the LLM-proxy client pattern (if subscription gating is needed in v1; otherwise deferred). The React renderer is treated as a visual reference for screen layout. `electron/`, `package.json`, `electron-builder` config, `tauri.conf.json`-equivalent files are not created.

## Why I am stopping here, not building

The user's plan requires checkpoint sign-off at the end of Phase 1. PySide6 is the technically correct answer to the question as posed, but it has two real costs the user should weigh consciously: (a) rebuilding the UI in Qt rather than reusing the React renderer, and (b) writing a bespoke auto-updater rather than inheriting `electron-updater`.

**If either cost is unacceptable, the next-best choice is Electron, not Tauri.** The math is close (3.45 vs 3.25) and reflects an honest tradeoff: Electron wins because we already have a clean main / preload / tray and a working React renderer, and because `electron-builder` + `electron-updater` is the most mature signing and update story for any of the sidecar options. Tauri wins on bundle size, security model, and modernity, but those advantages are small in the context of a single-user desktop tool, and resuming from the existing Electron shell saves real engineering days that Tauri would spend re-implementing.

**The case for Tauri** is real and would be the right pick if we were greenfield with no prior shell, or if the auto-update polish in Tauri (which is now better than it was a year ago) is given more weight than I gave it.

A separate note about the six documented divergences in `autoapply.com.au/debug-log.md`: those are in the in-tree Seek automation (`backend/automation/`), not in the Electron IPC layer. The audit's verdict on the Electron shell itself is "REUSE AS-IS." Resuming from that shell does not inherit Seek-scraping drift; resuming from `backend/automation/` would, which is exactly what ADR-0001 says we will not do.

## Consequences

- Engine remains untouched; adapter owns the three things the engine lacks (unified entry point, progress callback, real-submit gate).
- UI is built in Qt Widgets or QML. No React. We will lift screen layout patterns from the old renderer but no code carries over.
- Code-signing identities and certificates from the old project (if the user owns them) are reused; only the signing commands change.
- The auto-update story is a small bespoke component (around 200 to 500 lines) instead of a library, accepted explicitly as a tradeoff.
- The cross-platform parity rule (Windows from slice 1) is enforceable because PySide6, PyInstaller, `codesign`, and `signtool` all support both targets and the engine itself runs on both (with the LibreOffice and Chrome path branches added in the adapter, not the engine).
- If the user vetoes this, the documented fallback is Electron (resume the existing shell + renderer), with Tauri as a third option if a greenfield modern stack is preferred over salvage. ADR-0002 will be amended with a one-paragraph status update reflecting the chosen path.

## Status

Proposed. Awaiting user decision per the Phase 1 checkpoint in the original plan.
