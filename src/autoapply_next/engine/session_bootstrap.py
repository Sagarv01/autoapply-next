"""In-app Seek session bootstrap.

Replaces the old "run setup_sessions.py from a terminal" instruction with a
function the GUI worker can drive. The user clicks a button; we launch a
persistent Chromium against the engine's `sessions/seek_chrome_profile/`
user-data-dir, navigate to Seek's login page, and watch the URL until either
(a) the user signs in (URL leaves the login / oauth path), (b) the user
closes the browser, or (c) the GUI requests cancel. We then run a quick
headless verification: open the engine's profile page and check that the
response is a logged-in profile, not a login redirect.

We never see the user's password. The whole point of this flow is that the
user types credentials directly into Chromium, not through us.

The engine's legacy `sessions/seek/state.json` is also written so the
adapter's existing `Path("sessions/seek/state.json").exists()` precondition
in `applicator._apply_seek` keeps holding. The persistent user-data-dir
remains the source of truth for cookies; state.json is a marker file.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .adapter import _engine_workdir

logger = logging.getLogger(__name__)


SEEK_LOGIN_URL = "https://au.seek.com/oauth/login"
SEEK_VERIFY_URL = "https://au.seek.com/profile/me"
LOGIN_POLL_INTERVAL = 2.0  # seconds; matches setup_sessions.py cadence
LOGIN_MAX_WAIT_SECONDS = 600  # 10 minutes; we still bail if the user wanders
VERIFY_TIMEOUT_MS = 20000
LOGGED_IN_CONTENT_MARKERS = [
    "sign out",
    "signout",
    "my profile",
    "my account",
    "logged in",
    "dashboard",
    "/profile/",
]


class SessionStatus(str, Enum):
    VALID = "valid"
    """User logged in successfully and verification confirmed a live session."""

    INVALID = "invalid"
    """The browser closed but the verification probe was redirected to login."""

    ABANDONED = "abandoned"
    """The user closed the browser before login was detected. No cookies set."""

    CANCELLED = "cancelled"
    """The GUI requested cancel. Browser closed by us. Cookies may or may not be set."""

    ERROR = "error"
    """Something blew up. Look at `error_message` and the log."""


@dataclass(frozen=True)
class SessionBootstrapResult:
    status: SessionStatus
    message: str
    error_message: str | None = None


StatusCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]


async def run_session_bootstrap(
    *,
    engine_workdir: Path,
    on_status: StatusCallback | None = None,
    is_cancelled: CancelCheck | None = None,
) -> SessionBootstrapResult:
    """Open Chromium for the user to log into Seek, then verify the session.

    Threading: caller must run this on an asyncio loop owned by a non-GUI
    thread. Does not return until the user closes the browser or cancels.

    Cancellation: poll `is_cancelled()` while waiting; on True, close the
    context and return CANCELLED.
    """
    on_status = on_status or (lambda _msg: None)
    is_cancelled = is_cancelled or (lambda: False)

    engine_workdir = Path(engine_workdir).resolve()
    with _engine_workdir(engine_workdir):
        # Engine modules import lazily inside the cwd-bound block so their
        # module-level path constants resolve correctly.
        try:
            import seek_apply  # type: ignore[import-not-found]
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except Exception as exc:
            logger.exception("session_bootstrap: import failed")
            return SessionBootstrapResult(
                status=SessionStatus.ERROR,
                message="Could not load engine + Playwright",
                error_message=str(exc),
            )

        user_data_dir = engine_workdir / "sessions" / "seek_chrome_profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        legacy_state = engine_workdir / "sessions" / "seek" / "state.json"
        legacy_state.parent.mkdir(parents=True, exist_ok=True)

        # Phase 1: launch interactive browser. Reuse engine's Chrome-path
        # logic so Seek sees the same fingerprint the apply flow uses.
        launch_kwargs = _launch_kwargs(seek_apply, user_data_dir)
        try:
            on_status(f"Launching Chromium at {user_data_dir.name}/")
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                try:
                    on_status(
                        "Browser open. Log in to Seek in the window that just "
                        "appeared. Close the browser when you are done; the app "
                        "will then verify the session."
                    )
                    page = await context.new_page()
                    try:
                        await page.goto(
                            SEEK_LOGIN_URL,
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                    except Exception as exc:
                        logger.warning(
                            "session_bootstrap: initial nav failed (%s); "
                            "browser remains open for manual navigation",
                            exc,
                        )
                    detected_login_at: float | None = None
                    # Poll for either: (a) browser closed by user (context.pages empty
                    # AND _is_closed), (b) URL leaves login, (c) cancellation.
                    elapsed = 0.0
                    saw_logged_in = False
                    while True:
                        if is_cancelled():
                            on_status("Cancel requested. Closing browser.")
                            try:
                                await context.close()
                            except Exception:
                                pass
                            return SessionBootstrapResult(
                                status=SessionStatus.CANCELLED,
                                message="Session bootstrap cancelled by user.",
                            )
                        if not context.pages:
                            on_status("Browser closed.")
                            break
                        # If page was closed but context still has others, ours might
                        # be the only one. We poll the URL of the still-open page.
                        try:
                            current_pages = list(context.pages)
                            current_url = current_pages[0].url if current_pages else ""
                        except Exception:
                            current_url = ""
                        if current_url and _looks_logged_in_url(current_url):
                            if not saw_logged_in:
                                saw_logged_in = True
                                on_status(
                                    "Logged-in URL detected. Close the browser to "
                                    "save the session."
                                )
                        await asyncio.sleep(LOGIN_POLL_INTERVAL)
                        elapsed += LOGIN_POLL_INTERVAL
                        if elapsed > LOGIN_MAX_WAIT_SECONDS:
                            on_status(
                                "Timed out waiting for login. Closing browser."
                            )
                            try:
                                await context.close()
                            except Exception:
                                pass
                            break

                    # Save legacy state.json marker if context still alive.
                    if context.pages or saw_logged_in:
                        try:
                            await context.storage_state(path=str(legacy_state))
                        except Exception as exc:
                            logger.warning(
                                "session_bootstrap: storage_state save failed: %s",
                                exc,
                            )
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass

            if not saw_logged_in:
                # We never observed a logged-in URL. The browser closed without
                # login. We still verify because cookies might be set anyway,
                # but the most likely outcome is ABANDONED / INVALID.
                pass

            # Phase 2: headless verification. Open a fresh persistent
            # context against the same user-data-dir. Navigate to the
            # profile page and check.
            on_status("Verifying session...")
            verify_kwargs = dict(launch_kwargs)
            verify_kwargs["headless"] = True
            try:
                async with async_playwright() as pw:
                    context = await pw.chromium.launch_persistent_context(
                        **verify_kwargs
                    )
                    try:
                        page = await context.new_page()
                        await page.goto(
                            SEEK_VERIFY_URL,
                            wait_until="domcontentloaded",
                            timeout=VERIFY_TIMEOUT_MS,
                        )
                        await asyncio.sleep(2)
                        url = page.url.lower()
                        content = (await page.content()).lower()
                        looks_valid = (
                            "login" not in url
                            and "oauth" not in url
                            and any(m in content for m in LOGGED_IN_CONTENT_MARKERS)
                        )
                    finally:
                        try:
                            await context.close()
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning("session_bootstrap: verify failed: %s", exc)
                return SessionBootstrapResult(
                    status=SessionStatus.ERROR,
                    message="Could not verify session.",
                    error_message=str(exc),
                )

            if looks_valid:
                # Ensure the legacy marker file exists even if storage_state
                # failed above; the adapter's _apply_seek precondition needs it.
                if not legacy_state.exists():
                    legacy_state.write_text("{}", encoding="utf-8")
                return SessionBootstrapResult(
                    status=SessionStatus.VALID,
                    message=(
                        "Seek session is valid. The engine will use this for all "
                        "future runs."
                    ),
                )
            if saw_logged_in:
                # We saw a logged-in URL during the interactive phase but the
                # verify probe disagrees. Surface as INVALID with a hint.
                return SessionBootstrapResult(
                    status=SessionStatus.INVALID,
                    message=(
                        "Verification probe was redirected to login. Cookies "
                        "may not have been persisted. Try again."
                    ),
                )
            return SessionBootstrapResult(
                status=SessionStatus.ABANDONED,
                message=(
                    "Browser closed before login was detected. No session "
                    "saved."
                ),
            )
        except asyncio.CancelledError:
            return SessionBootstrapResult(
                status=SessionStatus.CANCELLED,
                message="Session bootstrap cancelled.",
            )
        except Exception as exc:
            logger.exception("session_bootstrap: unexpected error")
            return SessionBootstrapResult(
                status=SessionStatus.ERROR,
                message="Unexpected error during session bootstrap.",
                error_message=f"{type(exc).__name__}: {exc}",
            )


def _launch_kwargs(seek_apply_mod, user_data_dir: Path) -> dict:
    kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": False,
        "viewport": {"width": 1280, "height": 800},
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    chrome_path = getattr(seek_apply_mod, "CHROME_PATH", None)
    if chrome_path is not None and Path(chrome_path).exists():
        kwargs["executable_path"] = str(chrome_path)
    return kwargs


def _looks_logged_in_url(url: str) -> bool:
    """Match the engine's setup_sessions.py heuristic: any Seek URL that does
    not contain 'login' or 'oauth' counts as logged in. Crude but reliable."""
    if not url:
        return False
    lower = url.lower()
    if "login" in lower or "oauth" in lower:
        return False
    return "seek.com" in lower
