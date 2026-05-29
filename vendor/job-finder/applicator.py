import asyncio
import os
import logging
from pathlib import Path

from browser_use import Agent
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.anthropic.chat import ChatAnthropic

from models import JobListing
from seek_apply import ExternalApplyError, SeekApplyError, apply_seek_quick

logger = logging.getLogger(__name__)

APPLY_TIMEOUT = 300  # 5 minutes for browser-use fallback


class BoardBlockedError(Exception):
    """Raised when a CAPTCHA or rate limit is detected."""


async def apply(
    job: JobListing,
    resume_pdf: str,
    cover_pdf: str,
    candidate: dict,
    page=None,
) -> str:
    """
    Submits an application.
    - Seek Quick Apply: uses direct Playwright automation (reliable file upload).
      If `page` is provided (already open on the apply URL from peek check), reuses it.
    - Other boards: uses browser-use Agent.
    Returns 'applied' on success.
    Raises BoardBlockedError on CAPTCHA/rate-limit.
    Raises TimeoutError on timeout.
    """
    if job.board == "seek" and job.easy_apply:
        return await _apply_seek(job, resume_pdf, cover_pdf, candidate, page=page)
    return await _apply_browser_use(job, resume_pdf, cover_pdf, candidate)


async def _apply_seek(job: JobListing, resume_pdf: str, cover_pdf: str, candidate: dict, page=None) -> str:
    """Direct Playwright applicator for Seek Quick Apply."""
    session_state = Path("sessions/seek/state.json").resolve()
    if not session_state.exists():
        raise PermissionError("No Seek session — run setup_sessions.py")
    try:
        return await asyncio.wait_for(
            apply_seek_quick(job, resume_pdf, cover_pdf, candidate, str(session_state), page=page),
            timeout=APPLY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(f"Seek application timed out: {job.url}")
    except PermissionError as e:
        raise BoardBlockedError(f"Seek session expired: {e}") from e
    except ExternalApplyError:
        raise
    except SeekApplyError as e:
        raise Exception(f"Seek apply form error: {e}") from e


async def _apply_browser_use(job: JobListing, resume_pdf: str, cover_pdf: str, candidate: dict) -> str:
    """browser-use Agent for non-Seek boards. Needs an API key — the library's
    ChatAnthropic doesn't accept the `claude` CLI / Max subscription."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise BoardBlockedError(
            f"Non-Seek apply path triggered for {job.board} but no "
            f"ANTHROPIC_API_KEY is set. Either disable non-Seek scraping "
            f"or set an API key with credits."
        )

    llm = ChatAnthropic(
        model=os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    task = (
        f"Apply for the job at this URL: {job.url}\n\n"
        f"Candidate details:\n"
        f"  Name: {candidate['name']}\n"
        f"  Email: {candidate['email']}\n"
        f"  Phone: {candidate['phone']}\n\n"
        f"Resume PDF: {resume_pdf}\n"
        f"Cover letter PDF: {cover_pdf}\n\n"
        "Instructions:\n"
        "1. Navigate to the job URL.\n"
        "2. If you see a login or sign-in page, log in.\n"
        "3. Click the Apply button.\n"
        "4. Fill all required fields with the candidate details.\n"
        "5. Upload the resume PDF when a file upload field appears.\n"
        "6. If a cover letter upload field exists, upload the cover letter PDF.\n"
        "7. Submit the application and confirm success.\n"
        "If you encounter a CAPTCHA, output exactly: CAPTCHA DETECTED\n"
        "If login ultimately fails, output exactly: LOGIN FAILED"
    )

    session_state = Path(f"sessions/{job.board}/state.json").resolve()
    chrome_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    profile = BrowserProfile(
        storage_state=str(session_state) if session_state.exists() else None,
        executable_path=str(chrome_path) if chrome_path.exists() else None,
        headless=False,
    )
    agent = Agent(
        task=task,
        llm=llm,
        browser_profile=profile,
        use_thinking=False,
    )

    try:
        result = await asyncio.wait_for(agent.run(), timeout=APPLY_TIMEOUT)
        final = (result.final_result() or "").lower()
        if "captcha detected" in final:
            raise BoardBlockedError(f"CAPTCHA detected on {job.board}")
        if "login failed" in final:
            raise BoardBlockedError(f"Login failed on {job.board}")
        return "applied"
    except asyncio.TimeoutError:
        raise TimeoutError(f"Application timed out after {APPLY_TIMEOUT}s: {job.url}")
    except BoardBlockedError:
        raise
    except Exception as exc:
        msg = str(exc).lower()
        if "captcha" in msg or "rate limit" in msg or "blocked" in msg:
            raise BoardBlockedError(f"Board blocked ({job.board}): {exc}") from exc
        raise
