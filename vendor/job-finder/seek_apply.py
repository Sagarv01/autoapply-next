"""
Direct Playwright automator for Seek Quick Apply forms.
- Bypasses native file dialogs via set_input_files() on hidden inputs
- Verifies the correct file is selected before continuing
- Handles extra employer screening questions via Claude
- Verifies submission via Applied Jobs page
"""
import asyncio
import logging
import os
from pathlib import Path

from playwright.async_api import Page, async_playwright

from claude_cli import DEFAULT_MODEL, claude_complete
from models import JobListing
from utils import load_profile

logger = logging.getLogger(__name__)

CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
USER_DATA_DIR = Path("sessions/seek_chrome_profile").resolve()
APPLY_TIMEOUT = 180  # seconds


class SeekApplyError(Exception):
    pass


class ExternalApplyError(Exception):
    """Raised when the job redirects to an external application site (not Quick Apply)."""
    pass


class _PeekSession:
    """Reusable browser session for Quick Apply pre-checks. Uses a persistent
    Chrome user-data-dir (cookies, history, fingerprint of a real browser) so
    Seek can't trivially fingerprint us as Playwright."""
    _pw = None
    _ctx = None  # persistent context owns the browser; no separate _browser

    @classmethod
    async def get_page(cls, session_state: str):
        # session_state arg kept for backwards compat; ignored — we use user-data-dir now
        if cls._ctx is None or not cls._ctx.browser or not cls._ctx.browser.is_connected():
            if cls._pw is None:
                from playwright.async_api import async_playwright as _apw
                cls._pw = await _apw().start()
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            launch_kwargs: dict = {
                "user_data_dir": str(USER_DATA_DIR),
                "headless": False,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if CHROME_PATH.exists():
                launch_kwargs["executable_path"] = str(CHROME_PATH)
            cls._ctx = await cls._pw.chromium.launch_persistent_context(**launch_kwargs)
        return await cls._ctx.new_page()

    @classmethod
    async def close(cls):
        try:
            if cls._ctx:
                await cls._ctx.close()
            if cls._pw:
                await cls._pw.stop()
        except Exception:
            pass
        cls._pw = cls._ctx = None


async def peek_is_quick_apply(job_url: str, session_state: str) -> tuple[bool, object]:
    """
    Check if job is Quick Apply. Returns (is_quick, page).
    If True: page is left open on the apply URL — pass it to apply_seek_quick to avoid
    re-navigating. Caller must close the page when done.
    If False: page is already closed.

    JD fetching is intentionally NOT done here — the apply page hides the JD behind
    a "View job description" button and Seek's selectors don't match there. Use
    fetch_seek_jd against the listing page instead.
    """
    apply_url = job_url.rstrip("/") + "/apply"
    page = None
    try:
        page = await _PeekSession.get_page(session_state)
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.5)
        url = page.url.lower()
        if "seek.com" not in url or "login" in url or "oauth" in url:
            logger.info(f"  peek: not-quick (host/login redirect) {url[:120]}")
            await page.close()
            return False, None
        marker = page.locator('#resume-fileFile, input[name="resume-method"]')
        if await marker.count() == 0:
            logger.info(f"  peek: not-quick (Quick Apply marker missing) {url[:120]}")
            await page.close()
            return False, None
        # Leave page open — caller reuses it for the actual apply
        return True, page
    except Exception as e:
        logger.warning(f"  peek: navigation failed for {job_url} — {type(e).__name__}: {str(e)[:150]}")
        if page:
            try:
                await page.close()
            except Exception:
                pass
        return False, None


async def fetch_seek_jd(job_url: str, session_state: str = "") -> str:
    """Fetch JD text from a Seek LISTING page via the existing _PeekSession.

    Seek renders [data-automation="jobAdDetails"] server-side on the listing
    page (no /apply, no button click required). Returns "" on any failure.
    """
    base = job_url.split("?")[0].split("#")[0].rstrip("/")
    if base.endswith("/apply"):
        base = base[: -len("/apply")]
    page = None
    try:
        page = await _PeekSession.get_page(session_state)
        await page.goto(base, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_selector('[data-automation="jobAdDetails"]', timeout=8000)
        except Exception:
            pass
        el = page.locator('[data-automation="jobAdDetails"]')
        if not await el.count():
            logger.info(f"  fetch_seek_jd: jobAdDetails not present {base}")
            return ""
        text = (await el.first.inner_text()).strip()
        if len(text) < 100:
            logger.info(f"  fetch_seek_jd: too-short ({len(text)} chars) {base}")
            return ""
        return text[:5000]
    except Exception as e:
        logger.warning(f"  fetch_seek_jd failed for {base}: {type(e).__name__}: {str(e)[:150]}")
        return ""
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def apply_seek_quick(
    job: JobListing,
    resume_pdf: str,
    cover_pdf: str,
    candidate: dict,
    session_state: str,
    page=None,
) -> str:
    """
    Apply to a Seek Quick Apply job using Playwright directly.
    If `page` is provided (from peek_is_quick_apply), reuses it — no new browser launch,
    no re-navigation. Otherwise opens a fresh tab in the shared browser.
    Returns 'applied' on success.
    """
    resume_path = str(Path(resume_pdf).resolve())
    cover_path = str(Path(cover_pdf).resolve())
    resume_name = Path(resume_pdf).name
    cover_name = Path(cover_pdf).name

    own_page = page is None
    _Journal.begin(job)
    outcome = "failed"
    final_error: str | None = None
    try:
        if page is None:
            # Fallback: open a new tab in the shared browser
            apply_url = job.url.rstrip("/") + "/apply"
            page = await _PeekSession.get_page(session_state)
            logger.info(f"Seek apply: {apply_url}")
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            _assert_not_login(page)
            quick_apply_marker = page.locator('#resume-fileFile, input[name="resume-method"]')
            if await quick_apply_marker.count() == 0:
                raise ExternalApplyError(f"Not a Quick Apply form: {page.url}")
        else:
            logger.info(f"Seek apply (reusing tab): {page.url}")
            _assert_not_login(page)

        # ── Step 0: Clear any existing resumes from profile library ──────
        logger.info("Step 0: clearing old resumes from profile library")
        await _clear_profile_resumes(page)

        # ── Step 1: Choose documents ──────────────────────────────────────
        logger.info("Step 1: uploading documents")
        await _upload_resume(page, resume_path, resume_name)
        await _upload_cover_letter(page, cover_path, cover_name)
        await asyncio.sleep(5)  # let Seek finish processing both uploads before continuing
        await _click_continue(page)

        # ── Step 2+: Handle all intermediate steps until Review ───────────
        # When Continue is clicked but the page didn't advance, Seek shows
        # validation errors. Try multiple recovery strategies before giving up.
        # Every stuck question is logged to errors/stuck_questions.jsonl for
        # later pattern analysis (so we can build new rules for common cases).
        max_steps = 12
        max_recovery_attempts = 4
        prev_step = None
        repeats = 0
        reached_review = False
        for iteration in range(max_steps):
            step = await _current_step_text(page)
            logger.info(f"  Current step: '{step}' (iter {iteration})")
            _Journal.step(step)
            if "review" in step:
                reached_review = True
                break
            if step and step == prev_step:
                repeats += 1
                errors = await _find_validation_errors(page)
                _Journal.validation_errors(errors)
                if errors:
                    logger.warning(f"  Seek validation errors ({len(errors)}): "
                                   f"{[e['error'][:50] for e in errors]}")
                    # Strategy escalates with attempt number: try smarter prompts,
                    # then a random pick fallback, before logging + bailing.
                    fixed = await _resolve_validation_errors(
                        page, errors, job, attempt=repeats,
                    )
                    if fixed:
                        logger.info(f"  Resolved {fixed} field(s) per Seek's error message")
                        await _click_continue(page)
                        continue
                if repeats >= max_recovery_attempts:
                    # Log to file for pattern analysis + bail this single job
                    await _log_stuck_questions(job, step, errors, page=page)
                    err_summary = (
                        " | ".join(f"{e['question'][:60]} → {e['error'][:60]}" for e in errors)
                        if errors else "no validation message detected"
                    )
                    raise SeekApplyError(
                        f"Stuck on step '{step}' after {repeats + 1} recovery attempts. "
                        f"Logged to errors/stuck_questions.jsonl. Errors: {err_summary}"
                    )
            else:
                repeats = 0
                prev_step = step
            await _handle_form_step(page, job, candidate)
            await _click_continue(page)

        if not reached_review:
            raise SeekApplyError(
                f"Did not reach review step after {max_steps} iterations (final step: '{prev_step}'). "
                "Not force-submitting."
            )

        # ── Final: Review and submit ──────────────────────────────────────
        logger.info("Step final: submitting")
        await _submit(page)

        # ── Verify: check Applied Jobs page ──────────────────────────────
        verified = await _verify_applied(page, job.title, job.company)
        if verified:
            logger.info("✅ Verified on Applied Jobs page")
            outcome = "applied"
            return "applied"
        raise SeekApplyError(
            "Submission not verified on Applied Jobs page — likely failed (post-submit modal, "
            "validation error, or hidden required field). Marking as failed for manual retry."
        )

    except Exception as e:
        final_error = str(e)[:500]
        raise
    finally:
        _Journal.end(outcome, final_error)
        await asyncio.sleep(1)
        if page:
            try:
                await page.close()
            except Exception:
                pass


# ── Upload helpers ────────────────────────────────────────────────────────────

async def _clear_profile_resumes(page: Page):
    """
    Navigate to the Seek profile, open the resume drawer, and delete all
    existing resumes so the library has room for the new tailored one.
    Then returns to the apply page the caller was on.
    """
    apply_url = page.url  # remember where to go back

    try:
        await page.goto("https://au.seek.com/profile/me", wait_until="networkidle", timeout=20000)
        await asyncio.sleep(2)

        while True:
            btn = await page.query_selector('[data-automation="resume-edit-link"]')
            if not btn:
                break
            await btn.click()
            await asyncio.sleep(2)

            drawer = await page.query_selector('[data-automation="resume-form-drawer"]')
            if not drawer:
                break

            options_buttons = await drawer.query_selector_all('button[aria-label^="Options for"]')
            if not options_buttons:
                logger.info("  Resume library already clear.")
                break

            aria = await options_buttons[0].get_attribute("aria-label")
            logger.info(f"  Deleting resume: {aria}")
            await options_buttons[0].click()
            await asyncio.sleep(1)

            delete_option = await page.wait_for_selector(
                'button:has-text("Delete"), [role="menuitem"]:has-text("Delete")',
                timeout=5000,
            )
            await delete_option.click()
            await asyncio.sleep(1)

            try:
                confirm = await page.wait_for_selector('button:has-text("Delete"):visible', timeout=5000)
                await confirm.click()
                logger.info("  Deletion confirmed.")
            except Exception:
                pass

            await asyncio.sleep(2)

    except Exception as e:
        logger.warning(f"  Could not clear resumes: {e} — continuing anyway")

    # Navigate back to the apply page
    await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2)


async def _upload_resume(page: Page, resume_path: str, resume_name: str):
    """Upload resume and verify the correct filename is confirmed."""
    # Select "Upload a resumé" radio and wait for UI to re-render
    radio = page.locator('input[name="resume-method"][value="upload"]')
    if await radio.count():
        await radio.check()
        await asyncio.sleep(1.5)  # wait for Seek's React UI to show the file input

    # Inject file directly into hidden input (no native dialog)
    file_input = page.locator('#resume-fileFile')
    if await file_input.count() == 0:
        raise ExternalApplyError("No resume file input found — not a Quick Apply form")
    await file_input.set_input_files(resume_path)
    await asyncio.sleep(7)  # give Seek time to begin server-side upload before checking

    # Verify the exact new filename appears — no generic fallback
    await _wait_for_upload_confirmation(page, resume_name, "resume", timeout=10)


async def _upload_cover_letter(page: Page, cover_path: str, cover_name: str):
    """Upload cover letter and verify the correct filename is confirmed."""
    # Select "Upload a cover letter" radio and wait for UI to re-render
    radio = page.locator('input[name="coverLetter-method"][value="upload"]')
    if await radio.count():
        await radio.check()
        await asyncio.sleep(1.5)

    # Inject file directly
    file_input = page.locator('#coverLetter-fileFile')
    if await file_input.count():
        await file_input.set_input_files(cover_path)
        await asyncio.sleep(7)  # give Seek time to begin server-side upload before checking

    await _wait_for_upload_confirmation(page, cover_name, "cover letter")


def _assert_not_login(page: Page):
    """Raise PermissionError immediately if we've been redirected to login."""
    url = page.url.lower()
    if "login" in url or "oauth" in url or "signin" in url:
        raise PermissionError(
            "Seek session expired mid-apply — re-run setup_sessions.py"
        )


async def _wait_for_upload_confirmation(page: Page, filename: str, label: str, timeout: int = 5):
    """
    Wait up to `timeout` seconds for the exact uploaded filename to appear on the page.
    We do NOT accept generic 'attached' text — Seek pre-fills the form with the
    profile's last-used resume, so 'attached' is always present and cannot confirm
    that the NEW file was actually uploaded.
    Raises SeekApplyError if the exact filename is not found.
    """
    stem = Path(filename).stem[:25].lower()
    for _ in range(timeout):
        content = (await page.content()).lower()
        if stem in content:
            logger.info(f"  ✅ {label} confirmed: {filename}")
            await asyncio.sleep(25)  # wait for Seek to finish server-side processing
            return
        await asyncio.sleep(1)

    raise SeekApplyError(
        f"{label} upload not confirmed — '{stem}' not found on page after {timeout}s. "
        f"Seek may have used the profile's cached resume instead."
    )


# ── Step helpers ──────────────────────────────────────────────────────────────

async def _current_step_text(page: Page) -> str:
    """Return the current form step heading (lowercase)."""
    await asyncio.sleep(1)
    try:
        # Active breadcrumb button
        active = page.locator("button[aria-current='step'], button[aria-selected='true']")
        if await active.count():
            return (await active.first.inner_text()).strip().lower()
        # URL-based detection
        url = page.url.lower()
        if "/review" in url:
            return "review and submit"
        if "/profile" in url:
            return "update seek profile"
        if "/questions" in url:
            return "questions"
        # Page heading fallback
        heading = page.locator("h1, h2")
        if await heading.count():
            return (await heading.first.inner_text()).strip().lower()
    except Exception:
        pass
    return ""


async def _handle_form_step(page: Page, job: JobListing, candidate: dict):
    """
    Fill any visible form fields on the current step.
    Handles: standard profile fields + employer screening questions via Claude.
    """
    await asyncio.sleep(1)

    # Gather all visible form questions
    questions = await page.evaluate("""() => {
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]),' +
            'select, textarea'
        );
        return Array.from(inputs)
            .filter(el => el.offsetParent !== null)
            .map(el => {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                const section = el.closest('[data-automation], section, fieldset');
                return {
                    tag: el.tagName,
                    type: el.getAttribute('type') || '',
                    id: el.id,
                    name: el.name || '',
                    label: lbl?.innerText?.trim() || '',
                    placeholder: el.placeholder || '',
                    value: el.value || '',
                    options: el.tagName === 'SELECT'
                        ? Array.from(el.options).map(o => ({value: o.value, text: o.text}))
                        : [],
                    required: el.required,
                    sectionText: section?.innerText?.trim().slice(0, 100) || '',
                };
            });
    }""")

    # NOTE: we don't early-return when `questions` is empty — some Seek steps
    # have ONLY radio fieldsets or ONLY checkbox groups (e.g. visa-eligibility
    # questions), and those are detected/answered below. Early-returning here
    # would skip them.
    if questions:
        logger.info(f"  Found {len(questions)} form fields — answering...")
        for q in questions:
            if not q.get("label") and not q.get("placeholder") and not q.get("sectionText"):
                continue
            await _answer_field(page, q, job, candidate)

    # Also handle radio/checkbox questions (employer screening)
    radio_groups = await page.evaluate("""() => {
        const radios = document.querySelectorAll('input[type=radio]');
        const groups = {};
        radios.forEach(r => {
            if (!r.offsetParent) return;
            const name = r.name;
            if (!groups[name]) groups[name] = [];
            const lbl = document.querySelector(`label[for="${r.id}"]`);
            groups[name].push({
                id: r.id, value: r.value, checked: r.checked,
                label: lbl?.innerText?.trim() || r.value,
            });
        });
        return Object.entries(groups).map(([name, options]) => ({name, options}));
    }""")

    for group in radio_groups:
        # Skip resume/cover letter method radios (handled in Step 1)
        if "resume-method" in group["name"] or "coverLetter-method" in group["name"]:
            continue
        # Already has a selection? Skip.
        if any(o["checked"] for o in group["options"]):
            continue
        await _answer_radio_group(page, group, job, candidate)

    # Multi-select checkbox questions. Seek's apply form may render MULTIPLE
    # distinct checkbox groups in one step (one per employer question), so we
    # group by the input's `name` attribute (the natural group key — all
    # options for one question share the same name). The label lookup is
    # scoped to the input's nearest ancestor wrapper because Seek sometimes
    # emits duplicate `id` values across groups (e.g. "..._0", "..._1");
    # a page-wide `label[for=id]` query would return the wrong group's label.
    checkbox_groups = await page.evaluate("""() => {
        // Find a usable selector for a checkbox: prefer data-testid (always
        // unique on Seek), then fall back to id (sometimes duplicated).
        const sel = (cb) => {
            const tid = cb.getAttribute('data-testid');
            if (tid) return `input[data-testid="${tid}"]`;
            if (cb.id) return `[id="${cb.id}"]`;
            return null;
        };
        // Find the question heading text for a checkbox. Seek's apply form
        // typically renders the question as a <strong> (or <legend>) in a
        // sibling element that PRECEDES the options container. We walk up
        // the input's ancestor chain and, at each level, scan preceding
        // siblings for that heading element.
        const headingTextNear = (cb) => {
            let node = cb;
            for (let depth = 0; depth < 10 && node; depth++) {
                let sib = node.previousElementSibling;
                while (sib) {
                    const h = sib.querySelector?.(
                        'legend, strong, [role="heading"]'
                    ) || (sib.matches?.('legend, strong, [role="heading"]') ? sib : null);
                    if (h && h.innerText?.trim()) return h.innerText.trim();
                    // Also accept any sibling whose text content is a short
                    // labelish string (no inputs inside it).
                    if (sib.querySelector && !sib.querySelector('input, textarea, select')) {
                        const t = sib.innerText?.trim();
                        if (t && t.length > 0 && t.length < 250 && !t.includes('\\n\\n')) {
                            return t;
                        }
                    }
                    sib = sib.previousElementSibling;
                }
                node = node.parentElement;
            }
            return '';
        };
        // Per-option label: prefer a label inside the same option wrapper
        // (not page-wide — IDs can collide). Falls back to a wrapping
        // <label>, then to nearby text in siblings.
        const labelFor = (cb) => {
            // 1. Label wrapping the input directly.
            const wrap = cb.closest('label');
            if (wrap) {
                const t = wrap.innerText?.trim();
                if (t) return t;
            }
            // 2. Label with for=id inside the same immediate option wrapper.
            //    Scope to a small ancestor so a duplicate id elsewhere on
            //    the page doesn't return the wrong text.
            let scope = cb.parentElement;
            for (let i = 0; i < 4 && scope; i++) {
                if (cb.id) {
                    const lbl = scope.querySelector(`label[for="${CSS.escape(cb.id)}"]`);
                    if (lbl) {
                        const t = lbl.innerText?.trim();
                        if (t) return t;
                    }
                }
                // Any label inside this scope at all (single-option wrappers).
                const anyLbl = scope.querySelector('label');
                if (anyLbl) {
                    const t = anyLbl.innerText?.trim();
                    if (t) return t;
                }
                scope = scope.parentElement;
            }
            return cb.value || '';
        };
        const groups = {};
        document.querySelectorAll('input[type=checkbox]').forEach(cb => {
            if (!cb.offsetParent) return;
            const name = cb.name || cb.id || '';
            if (!name) return;
            if (!groups[name]) {
                // Use the FIRST checkbox of each group to derive the heading
                // — it's most likely to have the question text as a
                // preceding sibling chain.
                groups[name] = {
                    heading: headingTextNear(cb),
                    options: [],
                };
            }
            groups[name].options.push({
                id: cb.id,
                selector: sel(cb),
                label: labelFor(cb),
                checked: cb.checked,
            });
        });
        return Object.entries(groups).map(([name, g]) => ({name, ...g}));
    }""")

    if checkbox_groups:
        await _answer_checkbox_groups(page, checkbox_groups)


async def _pick_salary_option(page: Page, field_id: str, options: list):
    """Pick the salary range option closest to $110,000 AUD (market rate for Sagar's profile)."""
    import re
    TARGET = 110_000

    best_value = None
    best_distance = float("inf")

    for opt in options:
        text = (opt.get("text") or "").strip()
        text_lower = text.lower()
        if not text or "please select" in text_lower or text_lower.startswith("select"):
            continue

        # Extract all numbers from the option text (e.g. "$100,001 - $120,000" → [100001, 120000])
        numbers = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", text) if int(n.replace(",", "")) > 1000]
        if not numbers:
            continue

        # Use midpoint of range if two numbers, otherwise the single number
        midpoint = sum(numbers) / len(numbers)
        distance = abs(midpoint - TARGET)

        if distance < best_distance:
            best_distance = distance
            best_value = opt.get("value") or opt

    if best_value:
        # Use the safe helper: it validates `best_value` exists in the live
        # DOM before calling select_option, and caps the timeout at 5s so we
        # don't hang if the page's option list differs from what we scraped.
        await _safe_select_option(page, field_id, desired_value=str(best_value))


async def _pick_experience_option(page: Page, field_id: str, options: list, label: str):
    """Pick the years-of-experience dropdown option based on Sagar's background."""
    import re

    # Core skills Sagar has 4 years of experience in
    FOUR_YEAR_SKILLS = [
        "aws", "cloud", "devops", "linux", "terraform", "infrastructure", "ci/cd",
        "docker", "kubernetes", "python", "bash", "iac", "platform", "sre",
        "site reliability", "systems engineer", "automation", "amazon connect",
        "contact centre", "conversational ai", "lex",
    ]
    # Roles/skills where Sagar has minimal/no direct experience
    LOW_EXPERIENCE_SKILLS = [
        "security analyst", "penetration", "pen test", "soc analyst", "malware",
        "forensic", "compliance analyst", "data scientist", "machine learning",
        "java developer", ".net developer", "frontend", "react", "angular",
        "mobile", "ios", "android", "sap", "oracle dba", "salesforce",
    ]

    label_lower = label.lower()
    has_direct_exp = any(skill in label_lower for skill in FOUR_YEAR_SKILLS)
    has_low_exp = any(skill in label_lower for skill in LOW_EXPERIENCE_SKILLS)

    target_years = 4 if has_direct_exp else (1 if has_low_exp else 2)

    # Parse numbers from each option and pick closest to target
    best_value = None
    best_distance = float("inf")
    first_non_placeholder = None

    for opt in options:
        text = (opt.get("text") or "").strip()
        text_lower = text.lower()
        if not text or "please select" in text_lower or text_lower.startswith("select"):
            continue
        if first_non_placeholder is None:
            first_non_placeholder = opt.get("value") or opt

        numbers = [int(n) for n in re.findall(r"\b\d+\b", text)]
        if not numbers:
            # Handle "Less than 1 year" → treat as 0
            if "less than" in text_lower or "no experience" in text_lower or "< 1" in text_lower:
                numbers = [0]
            else:
                continue

        midpoint = sum(numbers) / len(numbers)
        distance = abs(midpoint - target_years)
        if distance < best_distance:
            best_distance = distance
            best_value = opt.get("value") or opt

    if best_value is None:
        best_value = first_non_placeholder

    if best_value:
        # Use the safe helper: it validates `best_value` exists in the live
        # DOM before calling select_option, and caps the timeout at 5s so we
        # don't hang if the page's option list differs from what we scraped.
        await _safe_select_option(page, field_id, desired_value=str(best_value))


async def _answer_field(page: Page, q: dict, job: JobListing, candidate: dict):
    """Fill a single text/select/textarea field."""
    label = (q.get("label") or q.get("placeholder") or q.get("sectionText") or "").lower()

    # Rule-based answers for common fields
    value = None
    if any(w in label for w in ["phone", "mobile", "contact number"]):
        value = candidate["phone"]
    elif any(w in label for w in ["first name"]):
        value = candidate["name"].split()[0]
    elif any(w in label for w in ["last name", "surname"]):
        value = " ".join(candidate["name"].split()[1:]) or candidate["name"]
    elif any(w in label for w in ["full name", "your name"]):
        value = candidate["name"]
    elif any(w in label for w in ["email"]):
        value = candidate["email"]
    elif any(w in label for w in ["how many years", "years of experience", "years' experience",
                                   "years experience", "years have you"]):
        if q["tag"] == "SELECT" and q.get("options"):
            await _pick_experience_option(page, q["id"], q["options"], label)
        else:
            el = page.locator(f'[id="{q["id"]}"]') if q["id"] else page.locator(f'[name="{q["name"]}"]')
            if await el.count():
                try:
                    existing = await el.first.input_value()
                    if not existing:
                        await el.first.fill("4")
                        logger.info(f"    Filled '{label}' = '4'")
                except Exception:
                    pass
        return
    elif any(w in label for w in ["salary", "expected", "remuneration", "compensation", "pay rate"]):
        if q["tag"] == "SELECT" and q.get("options"):
            await _pick_salary_option(page, q["id"], q["options"])
        else:
            el = page.locator(f'[id="{q["id"]}"]') if q["id"] else page.locator(f'[name="{q["name"]}"]')
            if await el.count():
                try:
                    existing = await el.first.input_value()
                    if not existing:
                        await el.first.fill("110000")
                        logger.info(f"    Filled '{label}' = '110000'")
                except Exception:
                    pass
        return
    elif any(w in label for w in ["notice period", "availability", "start date"]):
        value = "2 weeks"
    elif any(w in label for w in ["visa", "work rights", "right to work", "work authorisation",
                                   "citizenship", "residency status"]):
        value = "485 Temporary Graduate Visa (expires July 2028)"
    elif any(w in label for w in ["linkedin"]):
        value = ""  # Skip

    if value is not None:
        if q["tag"] == "SELECT" and q.get("options"):
            await _select_best_option(page, q["id"], q["options"], value)
        else:
            el = page.locator(f'[id="{q["id"]}"]') if q["id"] else page.locator(f'[name="{q["name"]}"]')
            if await el.count():
                try:
                    existing = await el.first.input_value()
                    if not existing:
                        await el.first.fill(value)
                        logger.info(f"    Filled '{label}' = '{value}'")
                except Exception:
                    pass
        return

    # Unknown field — use Claude to answer
    if q["tag"] == "SELECT" and q.get("options"):
        answer = await _claude_answer(label, [o["text"] for o in q["options"]], job)
        await _select_best_option(page, q["id"], q["options"], answer)
    elif q["tag"] in ("INPUT", "TEXTAREA") and q["type"] not in ("hidden", "file"):
        answer = await _claude_answer(label, None, job)
        if answer:
            el = page.locator(f'[id="{q["id"]}"]') if q["id"] else page.locator(f'[name="{q["name"]}"]')
            if await el.count():
                try:
                    existing = await el.first.input_value()
                    if not existing:
                        await el.first.fill(answer)
                        logger.info(f"    Claude filled '{label}' = '{answer}'")
                except Exception:
                    pass


async def _find_validation_errors(page) -> list[dict]:
    """
    After a failed Continue, Seek paints a validation message right next to
    each unfilled required field. Scan for those and pair each with the
    nearest question text + nearest input/select/radio-group id.

    Returns: [{question, error, field_id, field_kind}, ...]
    """
    return await page.evaluate("""() => {
        // Each Seek question lives in its own <fieldset> (or [role=group])
        // with its own <legend> as the question label, and the validation
        // error renders inside the same fieldset. The OLD approach walked
        // 10 ancestors up grabbing the first heading it found, which on
        // nested cards captured an OUTER question's heading (e.g. "Visa
        // Status" was returned for a "Notice Period - Please make a
        // selection" error). closest('fieldset') anchors to the right
        // container deterministically.
        const out = [];
        const seen = new Set();
        const errorSelectors = [
            '[role="alert"]',
            '[data-automation*="error"]',
            '[id$="-error"]',
            '[class*="errorMessage"]',
            '[aria-invalid="true"]',
        ];
        const errorEls = new Set();
        errorSelectors.forEach(s => document.querySelectorAll(s).forEach(e => {
            if (e.offsetParent !== null) errorEls.add(e);
        }));

        errorEls.forEach(err => {
            const text = (err.innerText || err.textContent || '').trim();
            if (!text || text.length > 250) return;

            // Anchor to the question's own fieldset/group.
            const container = err.closest('fieldset, [role="group"], [role="radiogroup"]');
            if (!container) return;
            const legend = container.querySelector('legend, [class*="legend"]');
            const input  = container.querySelector('input,select,textarea');
            if (!input) return;
            const fid = input.id || input.getAttribute('name') || '';
            if (!fid || seen.has(fid)) return;
            seen.add(fid);

            // Prefer the legend; fall back to nearest label.
            let question = '';
            if (legend) question = legend.innerText.trim();
            if (!question) {
                const lbl = container.querySelector('label');
                if (lbl) question = lbl.innerText.trim();
            }

            out.push({
                question: question.slice(0, 200),
                error: text,
                field_id: fid,
                field_kind: input.tagName.toLowerCase() + (input.type ? ':' + input.type : ''),
            });
        });
        return out;
    }""")


async def _resolve_validation_errors(page, errors: list[dict], job: JobListing,
                                      attempt: int = 1) -> int:
    """
    For each validation error, try to populate the missing field. Strategy
    escalates with attempt number:
      attempt 1: Claude with the exact question + error context
      attempt 2: Claude with a stricter "MUST PICK ONE OF" prompt
      attempt 3: Pick first non-placeholder option (for select/radio/checkbox)
      attempt 4+: Tick/fill anything plausible just to unstick the form
    Returns count of fields successfully populated.
    """
    fixed = 0
    for e in errors:
        q, fid, kind = e["question"], e["field_id"], e["field_kind"]
        if not fid:
            continue
        # Get options if it's a select or radio group
        options_text = await page.evaluate(
            """(fid) => {
                const el = document.getElementById(fid);
                if (!el) return [];
                if (el.tagName === 'SELECT') {
                    return Array.from(el.options).map(o => o.text).filter(t => t && !/please select|^select/i.test(t));
                }
                // Radio group
                const radios = document.querySelectorAll(`input[name="${el.name || fid}"]`);
                return Array.from(radios).map(r => {
                    const lbl = document.querySelector(`label[for="${r.id}"]`);
                    return lbl?.innerText?.trim() || r.value;
                });
            }""", fid,
        )

        # Pick an answer per strategy
        answer = None
        source = None
        # Strategy 0 (always tried first): hard-rule for candidate facts —
        # this covers the citizenship / work-rights / security-clearance /
        # notice-period / salary questions that would otherwise loop forever.
        options_lower = [o.lower() for o in (options_text or [])]
        hr_idx = _hard_rule_index(q, options_lower)
        if hr_idx is None:
            ql = (q or "").lower()
            is_work_rights_q = any(w in ql for w in [
                "visa", "work right", "right to work", "work authoris",
                "citizenship", "residency", "working right", "citizen",
                "work in australia",
            ])
            if is_work_rights_q:
                hr_idx = _visa_485_index(options_lower)
                if hr_idx is not None:
                    source = "485-alias"
                else:
                    # 485 not listed → "Other" is the truthful pick.
                    hr_idx = _visa_other_index(options_lower)
                    if hr_idx is not None:
                        source = "visa-other-fallback"
        else:
            source = "hard-rule"
        if hr_idx is not None:
            answer = options_text[hr_idx]
            logger.info(f"    [attempt {attempt}] hard-rule answer for '{q[:60]}': '{answer[:60]}'")
        elif attempt <= 2:
            try:
                if attempt == 1:
                    answer = await _claude_answer(q, options_text or None, job)
                else:
                    # Stricter prompt: explicit constraint
                    forced_q = f"REQUIRED FIELD. Pick exactly one. Question: {q}"
                    answer = await _claude_answer(forced_q, options_text or None, job)
                source = "claude"
            except Exception as ex:
                logger.warning(f"    Claude failed for '{q[:60]}': {ex}")
        elif attempt == 3 and options_text:
            # Pick first non-placeholder option
            for opt in options_text:
                if not opt.lower().startswith(("please select", "select")):
                    answer = opt
                    source = "first-option"
                    break
        elif options_text:
            # Last resort: pick the most generic safe option
            for opt in options_text:
                if any(safe in opt.lower() for safe in ("yes", "no", "n/a", "other", "not applicable")):
                    answer = opt
                    source = "safe-option"
                    break
            if not answer:
                answer = options_text[0]
                source = "fallback"

        if not answer:
            answer = "Yes" if not options_text else options_text[0]
            source = source or "fallback"
        _Journal.answered(
            question=q, source=source or "unknown",
            answer=answer, options=options_text or None,
        )

        # Apply the answer based on kind
        try:
            if kind.startswith("select"):
                await _select_best_option(page, fid, [{"text": o, "value": o} for o in options_text], answer)
                fixed += 1
            elif kind.startswith("input:radio") or "radiogroup" in kind:
                radios = await page.evaluate(
                    """(fid) => {
                        const el = document.getElementById(fid);
                        const name = (el && el.name) || fid;
                        return Array.from(document.querySelectorAll(`input[type=radio][name="${name}"]`)).map(r => ({
                            id: r.id,
                            label: (document.querySelector(`label[for="${r.id}"]`)?.innerText || r.value).trim()
                        }));
                    }""", fid,
                )
                picked = False
                for r in radios:
                    if answer.lower() in r["label"].lower() or r["label"].lower() in answer.lower():
                        await page.locator(f'[id="{r["id"]}"]').check()
                        picked = True
                        fixed += 1
                        break
                if not picked and radios:
                    # Tick the first option as last-resort
                    await page.locator(f'[id="{radios[0]["id"]}"]').check()
                    fixed += 1
            elif kind.startswith("input:checkbox"):
                # Check the first checkbox in the group, or any matching answer
                cbs = await page.evaluate(
                    """(fid) => {
                        const el = document.getElementById(fid);
                        const name = (el && el.name) || fid;
                        return Array.from(document.querySelectorAll(`input[type=checkbox][name="${name}"]`)).map(c => ({
                            id: c.id,
                            label: (document.querySelector(`label[for="${c.id}"]`)?.innerText || c.value).trim()
                        }));
                    }""", fid,
                )
                target = next(
                    (c for c in cbs if answer.lower() in c["label"].lower()
                     or any(s in c["label"].lower() for s in ("none", "no such"))),
                    cbs[0] if cbs else None,
                )
                if target:
                    await page.locator(f'[id="{target["id"]}"]').check()
                    fixed += 1
            else:
                el = page.locator(f'[id="{fid}"]')
                if await el.count():
                    await el.first.fill(answer)
                    fixed += 1
            logger.info(f"    [attempt {attempt}] Fixed '{q[:60]}' = '{answer[:60]}'")
        except Exception as ex:
            logger.warning(f"    Apply failed for '{q[:60]}': {ex}")
    return fixed


async def _log_stuck_questions(job: JobListing, step: str, errors: list[dict], page=None):
    """Append a stuck-question encounter to errors/stuck_questions.jsonl AND,
    when page is supplied, dump the full HTML + every visible form field to
    errors/stuck_dumps/ so we can see what the bot missed."""
    import json
    from datetime import datetime
    from pathlib import Path
    Path("errors").mkdir(exist_ok=True)
    Path("errors/stuck_dumps").mkdir(exist_ok=True)

    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_id = job.url.rstrip("/").split("/")[-1].split("?")[0]
    html_path = None
    fields_path = None
    screenshot_path = None

    if page is not None:
        try:
            html_path = f"errors/stuck_dumps/{ts_file}_{job_id}.html"
            Path(html_path).write_text(await page.content(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"  stuck-dump: failed to capture HTML: {e}")
            html_path = None
        try:
            fields = await page.evaluate("""() => {
                const out = [];
                const sel = 'input, select, textarea, button, [role="radio"],'
                    + ' [role="checkbox"], [role="combobox"], [role="textbox"],'
                    + ' [role="listbox"], [role="radiogroup"], [role="group"]';
                for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const cs = window.getComputedStyle(el);
                    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        role: el.getAttribute('role') || '',
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        label: (
                            el.getAttribute('aria-label')
                            || (el.labels && el.labels[0]?.innerText)
                            || el.closest('label')?.innerText
                            || el.placeholder
                            || ''
                        ).trim().slice(0, 200),
                        value: (el.value || '').toString().slice(0, 100),
                        checked: el.checked || el.getAttribute('aria-checked') === 'true',
                        required: el.required || el.getAttribute('aria-required') === 'true',
                        ariaInvalid: el.getAttribute('aria-invalid') === 'true',
                    });
                }
                return out;
            }""")
            fields_path = f"errors/stuck_dumps/{ts_file}_{job_id}.fields.json"
            Path(fields_path).write_text(json.dumps(fields, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"  stuck-dump: failed to capture fields: {e}")
            fields_path = None
        try:
            screenshot_path = f"errors/stuck_dumps/{ts_file}_{job_id}.png"
            await page.screenshot(path=screenshot_path, full_page=True, timeout=10000)
        except Exception as e:
            logger.warning(f"  stuck-dump: failed to capture screenshot: {e}")
            screenshot_path = None

    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "url": job.url,
        "title": job.title,
        "company": job.company,
        "step": step,
        "errors": errors,
        "html_dump": html_path,
        "fields_dump": fields_path,
        "screenshot": screenshot_path,
    }
    with open("errors/stuck_questions.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.info(f"  stuck-dump written: html={html_path} fields={fields_path}")


# ── Per-application journal ───────────────────────────────────────────────────
# One JSONL line per apply attempt in errors/applications.jsonl, capturing
# every question answered, every validation error seen, and the final
# outcome. This is the source of truth for "why did this job fail" — much
# richer than bot.log (which mixes all jobs together) or stuck_questions.jsonl
# (which only fires on max-recovery bail).
class _Journal:
    """Module-level singleton. Apply runs are sequential so a shared dict
    is fine. Each apply_seek_quick call resets and finalises it."""
    _data: dict | None = None

    @classmethod
    def begin(cls, job: JobListing):
        from datetime import datetime
        cls._data = {
            "ts_start": datetime.now().isoformat(timespec="seconds"),
            "url": job.url,
            "title": job.title,
            "company": job.company,
            "steps_seen": [],
            "validation_errors_seen": [],
            "questions_answered": [],
            "outcome": None,
            "final_error": None,
        }

    @classmethod
    def step(cls, step_name: str):
        if cls._data is None: return
        if step_name and step_name not in cls._data["steps_seen"]:
            cls._data["steps_seen"].append(step_name)

    @classmethod
    def validation_errors(cls, errors: list[dict]):
        if cls._data is None or not errors: return
        for e in errors:
            cls._data["validation_errors_seen"].append({
                "question":  e.get("question", "")[:200],
                "error":     e.get("error", "")[:250],
                "field_id":  e.get("field_id", ""),
                "field_kind": e.get("field_kind", ""),
            })

    @classmethod
    def answered(cls, *, question: str, source: str, answer: str,
                 options: list[str] | None = None):
        """source: 'hard-rule' | '485-alias' | 'claude' | 'fallback'."""
        if cls._data is None: return
        cls._data["questions_answered"].append({
            "question": (question or "")[:200],
            "source":   source,
            "answer":   (answer or "")[:200],
            "options":  options[:20] if options else None,
        })

    @classmethod
    def end(cls, outcome: str, final_error: str | None = None):
        if cls._data is None: return
        from datetime import datetime
        import json
        from pathlib import Path
        cls._data["ts_end"] = datetime.now().isoformat(timespec="seconds")
        cls._data["outcome"] = outcome
        cls._data["final_error"] = final_error
        Path("errors").mkdir(exist_ok=True)
        try:
            with open("errors/applications.jsonl", "a") as f:
                f.write(json.dumps(cls._data) + "\n")
        except Exception as e:
            logger.warning(f"  Journal write failed: {e}")
        cls._data = None


async def _check_radio(page: Page, option: dict):
    """Tick a single radio option by id and log it."""
    radio = page.locator(f'[id="{option["id"]}"]')
    if await radio.count():
        try:
            await radio.check()
            logger.info(f"    Radio (rule) '{option['label']}' selected")
        except Exception as e:
            logger.warning(f"    Radio check failed for '{option['label']}': {e}")


# ── Hard-rule answer table ────────────────────────────────────────────────────
# Candidate facts (deterministic — never call Claude for these):
#   - NOT an Australian citizen or permanent resident
#   - HAS work rights (485 Temporary Graduate Visa, expires 8 July 2028)
#   - NO security clearance of any level
# Order matters: a question like "Are you an Australian Citizen, with a minimum
# NV1 Security Clearance?" matches BOTH the citizenship rule and the security-
# clearance rule. Citizenship runs first because "No" is the safe answer that
# satisfies the page's required-field check.
HARD_RULES = (
    # (label-substrings-any-of, option-substring-to-pick)
    (("australian citizen", "permanent resident",
      "are you a citizen", "citizen of australia",
      "citizenship status", "do you hold pr"),                "no"),
    (("legally entitled to work", "right to work",
      "eligible to work", "work authoris", "authorised to work",
      "work in australia"),                                   "yes"),
    (("security clearance", "nv1", "nv2",
      "baseline clearance", "negative vetting",
      "afp clearance", "agsva", "police clearance"),          "no"),
    (("notice period",),                                      "2 week"),
    (("salary expect", "expected salary", "remuneration"),    "negotiab"),
)

# Visa/485 aliases — match against radio/checkbox option text. The candidate
# is on a 485 Temporary Graduate visa, which Seek employer dropdowns spell
# many different ways.
VISA_485_ALIASES = (
    "485", "subclass 485",
    "temporary graduate", "graduate visa",
    "temporary visa with no restriction", "visa with no restriction",
    "no work restriction", "no restrictions",
    "post-study work", "post study work",
    "work visa", "valid visa",
)


def _hard_rule_index(question_label: str, options_lower: list[str]) -> int | None:
    """Return the index of the option that matches a hard-rule answer for
    this question, or None if no rule applies. Pure function — easy to
    unit-test against real labels from errors/stuck_questions.jsonl."""
    ql = (question_label or "").lower()
    if not options_lower:
        return None
    for needles, pick in HARD_RULES:
        if any(n in ql for n in needles):
            for i, opt in enumerate(options_lower):
                if pick in opt:
                    return i
            # Rule matched the question but its preferred option text is
            # missing — caller falls through to other strategies.
            return None
    return None


def _visa_485_index(options_lower: list[str]) -> int | None:
    """Return the index of the first option matching any 485-visa alias."""
    for i, opt in enumerate(options_lower):
        if any(a in opt for a in VISA_485_ALIASES):
            return i
    return None


# Option-label substrings that would be wrong for a 485 holder. We must
# never pick these on a work-rights question — they're either factually
# untrue (citizen/PR) or misleading (require sponsorship, since 485
# does not require employer sponsorship).
_VISA_DISQUALIFY_SUBSTRINGS = (
    "citizen", "permanent resident", "australian pr",
    "require sponsorship", "need sponsorship", "sponsorship required",
    "student visa", "working holiday",
)


def _visa_other_index(options_lower: list[str]) -> int | None:
    """Fallback for work-rights radio groups when no 485 alias matches.

    The candidate's actual visa (485 Temporary Graduate Visa) is often
    absent from employer-provided option lists. In that case "Other" is
    the truthful pick — never "Australian Citizen / Permanent Resident"
    or "Require Sponsorship".

    Returns the index of "Other" (or an "other"-like option) if present,
    else None so the caller can fall through.
    """
    # Exact match first ("Other", "other"), then substring ("Other visa",
    # "Other - please specify"). We require the option NOT to be one of
    # the disqualified labels, so "Other than Citizen" still wins but
    # "Other Australian Citizen" (unlikely but possible) would not.
    for i, opt in enumerate(options_lower):
        if opt.strip() in ("other", "other - please specify", "other (please specify)"):
            return i
    for i, opt in enumerate(options_lower):
        if "other" in opt and not any(d in opt for d in _VISA_DISQUALIFY_SUBSTRINGS):
            return i
    return None


async def _answer_radio_group(page: Page, group: dict, job: JobListing, candidate: dict):
    """Select the best radio option for a group."""
    options_text = [o["label"] for o in group["options"]]
    options_lower = [o.lower() for o in options_text]
    question_label = (group.get("label") or group.get("name") or "").lower()

    is_work_rights_q = any(w in question_label for w in [
        "visa", "work right", "right to work", "work authoris", "citizenship",
        "residency", "working right", "citizen", "work in australia"
    ])

    # ── Candidate-personal questions (gender / aboriginal / disability / veteran) ──
    # These were previously routed to Claude which often picked wrong (e.g. "Female").
    # Hard-rule them here based on candidate facts.
    def _pick(needle):
        return next((i for i, o in enumerate(options_lower) if needle in o), None)

    if "gender" in question_label or question_label.strip() == "sex":
        idx = _pick("male")  # picks "Male" (and avoids "Female" / "Non-binary")
        if idx is not None:
            await _check_radio(page, group["options"][idx])
            return
    if any(w in question_label for w in ["aboriginal", "torres strait", "indigenous"]):
        idx = next((i for i, o in enumerate(options_lower) if o.strip() in ("no", "neither")), None)
        if idx is not None:
            await _check_radio(page, group["options"][idx])
            return
    if any(w in question_label for w in ["disability", "long-term health condition"]):
        idx = next((i for i, o in enumerate(options_lower) if o.strip() == "no"), None)
        if idx is not None:
            await _check_radio(page, group["options"][idx])
            return
    if any(w in question_label for w in ["veteran", "defence force", "military service"]):
        idx = next((i for i, o in enumerate(options_lower) if o.strip() == "no"), None)
        if idx is not None:
            await _check_radio(page, group["options"][idx])
            return

    # 1. Hard-rule answers for candidate facts (citizenship, work rights,
    #    security clearance, notice period, salary). These cover the
    #    questions that previously dominated errors/stuck_questions.jsonl.
    best_idx = _hard_rule_index(question_label, options_lower)
    if best_idx is not None:
        await _check_radio(page, group["options"][best_idx])
        logger.info(f"    Radio (hard-rule) for '{question_label[:60]}': "
                    f"'{group['options'][best_idx]['label']}'")
        _Journal.answered(
            question=question_label, source="hard-rule",
            answer=group["options"][best_idx]["label"], options=options_text,
        )
        return

    # 2. Visa/work-rights multi-option dropdowns — pick the 485 alias if any
    #    option matches (e.g. "Temporary Graduate Visa", "Visa with no
    #    restrictions", "485 Subclass").
    best_idx = _visa_485_index(options_lower) if is_work_rights_q else None
    pick_source = "485-alias" if best_idx is not None else None

    # 2b. Work-rights question with NO 485 alias present (e.g. options are
    #     {Citizen/PR, Other, Require Sponsorship, Student Visa, Working
    #     Holiday Visa}). The candidate's 485 isn't listed → "Other" is
    #     the truthful pick. NEVER fall through to picking Citizen/PR or
    #     sponsorship, which would either be a lie or trigger an auto-reject.
    if best_idx is None and is_work_rights_q:
        best_idx = _visa_other_index(options_lower)
        if best_idx is not None:
            pick_source = "visa-other-fallback"

    # 3. Generic yes/no for non-citizenship/non-rights questions.
    if best_idx is None and any("yes" in o for o in options_lower):
        is_citizenship_q = is_work_rights_q or any(
            w in " ".join(options_lower) for w in ["citizen", "permanent resident", "pr ", "passport"]
        )
        if not is_citizenship_q:
            best_idx = next(i for i, o in enumerate(options_lower) if "yes" in o)
            pick_source = "yes-default"

    if best_idx is None:
        # Ask Claude with accurate visa context
        answer = await _claude_answer(
            f"Choose from: {', '.join(options_text)}", options_text, job
        )
        for i, o in enumerate(options_lower):
            if answer.lower() in o or o in answer.lower():
                best_idx = i
                pick_source = "claude"
                break
        if best_idx is None:
            best_idx = 0  # Default to first option
            pick_source = "fallback"

    if best_idx is not None:
        option = group["options"][best_idx]
        radio = page.locator(f'[id="{option["id"]}"]')
        if await radio.count():
            try:
                await radio.check()
                logger.info(f"    Radio '{option['label']}' selected")
                _Journal.answered(
                    question=question_label, source=pick_source or "unknown",
                    answer=option["label"], options=options_text,
                )
            except Exception as e:
                logger.warning(f"    Radio check failed for '{option['label']}': {e}")


_OPT_OUT_PATTERNS = (
    "none of these", "none of the above", "no such certification",
    "no such qualification", "i do not have", "not applicable",
    "no, i", "no, none", "neither",
    # Seek's checkbox groups often phrase the opt-out as "No - I am eligible"
    # (security clearance), "No - I require ..." (working arrangement),
    # "No MSP experience", or just bare "None". These weren't covered by
    # the comma variants above so the bot would leave the group blank and
    # Continue would silently fail.
    "no - i", "no -", "no — i", "none",
    "no msp", "no experience", "no relevant",
)
_TECH_KEYWORDS = (
    "aws", "amazon web services", "azure", "microsoft", "togaf", "cisco",
    "palo alto", "ec-council", "isc2", "isaca", "comptia", "nist",
    "google cloud", "gcp", "oracle", "salesforce", "hashicorp", "kubernetes",
    "linux", "red hat", "vmware",
)
_LEVEL_KEYWORDS = (
    "associate", "professional", "expert", "foundation", "practitioner",
    "specialist", "fundamentals",
)


def _option_matches_profile(heading: str, option_label: str, profile: str) -> bool:
    """True if this checkbox option corresponds to a cert/skill in the profile."""
    import re
    label = option_label.lower()
    head = heading.lower()
    prof = profile.lower()

    if any(p in label for p in _OPT_OUT_PATTERNS):
        return False

    # 1. Strip generic prefixes ("Yes, I have a", "Yes, I am") and check the
    #    cleaned cert name as a substring of the profile. Only count this as
    #    a hit if the cleaned label is multi-word (a generic single word like
    #    "associate" appears in too many false-positive contexts).
    cleaned = re.sub(r"^(yes,?\s+i\s+(have|am)\s+(an?\s+)?|i\s+have\s+(an?\s+)?)", "", label).strip()
    cleaned = re.sub(r"\s+(certified|certification)\s*$", "", cleaned).strip()
    if len(cleaned.split()) >= 2 and len(cleaned) > 8 and cleaned in prof:
        return True

    # 2. Tech keyword from heading + level keyword from option must appear in
    #    the same sentence/line of the profile, with at most 60 chars between
    #    them (tight enough to require an actual cert phrase like
    #    "AWS Certified ... Associate", not loose co-occurrence).
    techs = [k for k in _TECH_KEYWORDS if k in head]
    # Expand vendor aliases so "Amazon Web Services" in the heading also
    # matches "AWS" in the profile (and vice versa).
    aliases = {
        "amazon web services": "aws", "aws": "amazon web services",
        "microsoft azure": "azure", "azure": "microsoft azure",
        "google cloud": "gcp", "gcp": "google cloud",
    }
    for t in list(techs):
        if t in aliases:
            techs.append(aliases[t])
    levels = [k for k in _LEVEL_KEYWORDS if k in label]
    if techs and levels:
        for tech in techs:
            for lv in levels:
                pat = re.compile(
                    rf"{re.escape(tech)}[^.\n]{{0,60}}\b{re.escape(lv)}\b",
                    re.IGNORECASE,
                )
                if pat.search(prof):
                    return True
    return False


def _checkbox_locator(page: Page, opt: dict):
    """Resolve a Playwright locator for one checkbox option.

    Prefers the `selector` field (data-testid based — always unique) and
    falls back to id. Seek occasionally emits duplicate IDs across multiple
    checkbox groups in the same step, so id-only targeting can tick the
    wrong group's option.
    """
    selector = opt.get("selector")
    if selector:
        loc = page.locator(selector)
        return loc
    return page.locator(f'[id="{opt["id"]}"]')


def _checkbox_hard_rule_index(heading: str, options_lower: list[str]) -> int | None:
    """Hard-rule pick for a checkbox group, based on candidate facts.

    Mirrors `_hard_rule_index` for radio groups but adds rules for the
    question patterns that appear ONLY as checkbox groups on Seek
    (security clearance multi-select, years-of-experience buckets,
    Canberra/onsite working arrangements). Pure function — easy to unit
    test against the captured stuck-dump headings.

    Returns the index of the option to tick, or None if no rule applies.
    """
    import re
    ql = (heading or "").lower()
    if not options_lower:
        return None

    def _find(needle):
        for i, opt in enumerate(options_lower):
            if needle in opt:
                return i
        return None

    # ── Security clearance: candidate has NONE. Prefer an explicit
    #    "No - I am eligible" / "None" / "I do not hold" option over any
    #    specific clearance level (Baseline / NV1 / NV2 / TSPV).
    if any(w in ql for w in (
        "security clearance", "government security", "nv1", "nv2",
        "baseline clearance", "negative vetting", "agsva", "afp clearance",
    )):
        for needle in ("no - i", "no, i", "no — i", "none", "i do not",
                       "not applicable", "no clearance"):
            idx = _find(needle)
            if idx is not None:
                return idx
        return None

    # ── Working arrangement / Canberra onsite: candidate is Sydney-based,
    #    so prefer a "hybrid" / "remote" / "not based in Canberra" option,
    #    never "I am Canberra based" or "interstate but can attend".
    if any(w in ql for w in (
        "canberra office", "canberra based", "work onsite",
        "working arrangement", "on-site", "onsite at the client",
    )):
        for needle in ("no - i require", "no, i require",
                       "full hybrid", "fully remote", "remote work",
                       "no - i am", "no, i am"):
            idx = _find(needle)
            if idx is not None:
                return idx
        # Last resort: a generic "No" option (we don't want to pick a
        # Canberra-based or interstate-commute option).
        for i, opt in enumerate(options_lower):
            stripped = opt.strip()
            if stripped == "no" or stripped.startswith("no "):
                if "canberra" not in stripped and "interstate" not in stripped:
                    return i
        return None

    # ── Years of experience: candidate has 5+ years in core skills
    #    (AWS/DevOps/cloud/platform/SRE). Pick the highest bucket, but
    #    only if the question is about a skill the candidate actually has.
    if "years" in ql and (
        "experience" in ql or "exp" in ql
    ):
        # Skip if this is about an MSP-specific role (candidate has no
        # MSP experience — caller's profile-matcher handles those).
        skill_words = ql
        candidate_strong = any(s in skill_words for s in (
            "aws", "devops", "cloud", "platform", "sre", "linux",
            "python", "automation", "infrastructure", "ci/cd", "iac",
            "kubernetes", "docker", "terraform", "technical lead",
            "team lead", "lead engineer",
        ))
        if candidate_strong:
            # Pick the highest "X+ years" option.
            best_idx, best_num = None, -1
            for i, opt in enumerate(options_lower):
                m = re.search(r"(\d+)\s*\+", opt)
                if m and int(m.group(1)) > best_num:
                    best_num = int(m.group(1))
                    best_idx = i
            if best_idx is not None:
                return best_idx
            # Fall back to the option containing the highest "X-Y years"
            # range upper bound.
            best_idx, best_num = None, -1
            for i, opt in enumerate(options_lower):
                nums = [int(n) for n in re.findall(r"\d+", opt)]
                if nums and max(nums) > best_num:
                    best_num = max(nums)
                    best_idx = i
            return best_idx
        return None

    # ── Driver's licence + own car (Milan question): candidate has one.
    if "drivers licen" in ql or "driver's licen" in ql or "valid licence" in ql:
        idx = _find("yes")
        if idx is not None:
            return idx

    # ── Location-in-Perth / state-specific location questions: candidate
    #    is Sydney-based, so the truthful answer is "No".
    if "currently located in perth" in ql or "based in perth" in ql:
        idx = _find("no")
        if idx is not None:
            return idx

    # ── Work-rights / employment-rights checkbox question. The candidate
    #    holds a 485 Temporary Graduate Visa (HAS the right to work, but
    #    NOT a citizen/PR). Prefer "temporary work rights" / "485" /
    #    "graduate visa" over "permanent" (lie) or "require sponsorship"
    #    (untrue: 485 does not require employer sponsorship). We do our
    #    own match rather than calling _visa_485_index, because that
    #    helper's "no restrictions" alias is too broad — it would also
    #    match "permanent work rights with no restrictions".
    if any(w in ql for w in (
        "employment rights", "right to work", "work rights",
        "work in australia", "work authoris", "authorised to work",
        "best describes your right",
    )):
        prefer_strong = (
            "485", "subclass 485", "graduate visa", "temporary graduate",
            "post-study work", "post study work",
        )
        prefer_temp = ("temporary",)
        disqualify = (
            "permanent", "citizen", "australian pr",
            "require sponsorship", "need sponsorship",
        )
        # Strong preference: explicit 485 alias.
        for i, opt in enumerate(options_lower):
            if any(a in opt for a in prefer_strong):
                if not any(d in opt for d in disqualify):
                    return i
        # Second: "temporary" + "no restriction" / unconditional.
        for i, opt in enumerate(options_lower):
            if "temporary" in opt and "no restriction" in opt:
                if not any(d in opt for d in disqualify):
                    return i
        # Third: any "temporary" option that isn't disqualified.
        for i, opt in enumerate(options_lower):
            if any(a in opt for a in prefer_temp):
                if not any(d in opt for d in disqualify):
                    return i
        # Third: "other" fallback.
        idx = _visa_other_index(options_lower)
        if idx is not None:
            return idx
        # Fall through to the shared radio table (which would pick "yes"
        # if present, "no" for citizenship phrasing, etc.).

    # ── Generic citizenship / work-rights checkbox phrasings (rare; usually
    #    rendered as radios, but Seek occasionally uses checkboxes). Defer
    #    to the shared radio hard-rule table so behaviour stays consistent.
    rr = _hard_rule_index(heading, options_lower)
    if rr is not None:
        return rr
    return None


async def _answer_checkbox_groups(page: Page, groups: list):
    """Tick checkboxes that match the profile; otherwise tick the opt-out option.

    Handles multi-group steps where Seek renders several distinct checkbox
    questions side-by-side (e.g. security clearance + working arrangement +
    years of experience on the same page). Each group is identified by its
    shared `name` attribute. For each group we try, in order:

      1. Candidate-specific hard rule (`_checkbox_hard_rule_index`) — fires
         for clearance / years / location-arrangement / yes-no questions.
      2. Profile match (`_option_matches_profile`) — fires for cert-list
         multi-selects ("Which AWS certifications do you hold?" etc.).
      3. Opt-out option ("None of these", "No - I am eligible", ...).
    """
    profile = load_profile().strip()
    if not profile:
        logger.warning("    Profile text empty — cannot match checkbox certs")
    for g in groups:
        if any(o["checked"] for o in g["options"]):
            continue  # User-already-answered or pre-filled
        heading = g.get("heading", "")
        options_lower = [o["label"].lower() for o in g["options"]]
        logger.info(
            f"  Checkbox group '{heading[:80]}' ({len(g['options'])} options)"
        )

        # 1. Candidate-specific hard rule.
        hr_idx = _checkbox_hard_rule_index(heading, options_lower)
        if hr_idx is not None:
            opt = g["options"][hr_idx]
            try:
                await _checkbox_locator(page, opt).check()
                logger.info(
                    f"    ✅ hard-rule ticked: {opt['label']!r} "
                    f"(group '{heading[:60]}')"
                )
                _Journal.answered(
                    question=heading,
                    source="checkbox-hard-rule",
                    answer=opt["label"],
                    options=[o["label"] for o in g["options"]],
                )
            except Exception as e:
                logger.warning(
                    f"    Hard-rule check failed for {opt['label']!r}: {e}"
                )
            continue

        # 2. Profile-match (multi-select cert lists).
        matched = [
            o for o in g["options"]
            if _option_matches_profile(heading, o["label"], profile)
        ]
        if matched:
            for opt in matched:
                try:
                    await _checkbox_locator(page, opt).check()
                    logger.info(f"    ✅ matched profile, ticked: {opt['label']}")
                except Exception as e:
                    logger.warning(f"    Check failed for '{opt['label']}': {e}")
            continue

        # 3. Opt-out fallback.
        opt_out = next(
            (o for o in g["options"]
             if any(p in o["label"].lower() for p in _OPT_OUT_PATTERNS)),
            None,
        )
        if opt_out:
            try:
                await _checkbox_locator(page, opt_out).check()
                logger.info(
                    f"    ⊘ no profile match, ticked opt-out: {opt_out['label']}"
                )
            except Exception as e:
                logger.warning(f"    Opt-out check failed: {e}")
        else:
            logger.warning(
                f"    ⚠️  no profile match AND no opt-out option for "
                f"'{heading[:80]}' — group left blank, form may reject"
            )


# ── FIX-C: robust select_option helpers ─────────────────────────────────────
#
# Bug: Playwright's `locator.select_option(value=X)` retries silently for 30s
# when X isn't in the live <option> list, then raises TimeoutError. That
# consumed almost the whole APPLY_TIMEOUT on Seek's D365 Application Support
# Consultant role (jobs 92225429 / 92225603) because the value we passed for
# question_70 didn't match any actual option.
#
# Fix: always enumerate the live DOM options first, fuzzy-match against them,
# and call select_option with a short timeout so we fail fast on miss.

# Cap select_option calls so they can never burn the entire APPLY_TIMEOUT.
SELECT_OPTION_TIMEOUT_MS = 5000


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings (small, stdlib-only)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _is_placeholder_option(text: str, value: str = "") -> bool:
    """Identify Seek's 'Please select' / blank placeholder options."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if "please select" in t or t.startswith("select "):
        return True
    if t in ("select", "-", "--", "choose", "please choose"):
        return True
    return False


def _pick_best_real_option(real_options: list[dict], target: str) -> dict | None:
    """
    Given the live <option> list scraped from the DOM, pick the option whose
    text best matches `target`. Falls back through:
      exact text → substring → keyword overlap → Levenshtein → safe
      defaults (None of the above / No / Other / N/A) → first non-placeholder.

    Returns the chosen option dict ({'value','text'}) or None if nothing
    selectable exists.
    """
    if not real_options:
        return None

    target_lower = (target or "").strip().lower()
    target_words = set(target_lower.split())

    candidates = [
        o for o in real_options
        if not _is_placeholder_option(o.get("text", ""), o.get("value", ""))
    ]
    if not candidates:
        return None

    if target_lower:
        # Pass 1: exact text match (case-insensitive)
        for o in candidates:
            if (o.get("text") or "").strip().lower() == target_lower:
                return o

        # Pass 2: substring (either direction)
        for o in candidates:
            text = (o.get("text") or "").strip().lower()
            if text and (target_lower in text or text in target_lower):
                return o

        # Pass 3: keyword overlap
        best_score, best_o = 0, None
        for o in candidates:
            text = (o.get("text") or "").strip().lower()
            score = len(target_words & set(text.split()))
            if score > best_score:
                best_score, best_o = score, o
        if best_o is not None:
            return best_o

        # Pass 4: closest by Levenshtein (only if reasonably close)
        best_dist, best_o = None, None
        for o in candidates:
            text = (o.get("text") or "").strip().lower()
            if not text:
                continue
            d = _levenshtein(target_lower, text)
            cutoff = max(len(target_lower), len(text)) // 2 + 1
            if d <= cutoff and (best_dist is None or d < best_dist):
                best_dist, best_o = d, o
        if best_o is not None:
            return best_o

    # Pass 5: safe defaults — "None of the above" / "No" / "Other" / etc.
    safe_keywords = (
        "none of the above", "none of these", "no such",
        "not applicable", "n/a", "prefer not", "other", "none", "no",
    )
    for kw in safe_keywords:
        for o in candidates:
            text = (o.get("text") or "").strip().lower()
            if text == kw or text.startswith(kw + " ") or text == kw + ".":
                return o

    # Pass 6: first non-placeholder option (always pick *something*)
    return candidates[0]


async def _list_real_select_options(page: Page, field_id: str) -> list[dict]:
    """Scrape the live <option> list from the DOM. Returns [{value, text}, ...]."""
    if not field_id:
        return []
    try:
        return await page.evaluate(
            """(fid) => {
                const el = document.getElementById(fid);
                if (!el || el.tagName !== 'SELECT') return [];
                return Array.from(el.options).map(o => ({
                    value: o.value, text: (o.text || '').trim(),
                }));
            }""",
            field_id,
        )
    except Exception as exc:
        logger.warning(f"    Could not enumerate options for #{field_id}: {exc}")
        return []


async def _safe_select_option(
    page: Page,
    field_id: str,
    desired_value: str | None = None,
    desired_text: str | None = None,
) -> bool:
    """
    Robustly pick an option on a <select>:

    1. Enumerate the actual <option> elements in the DOM.
    2. If `desired_value` matches a real option's `value`, use it directly.
    3. Otherwise, fuzzy-match against the live option text using
       `_pick_best_real_option`.
    4. Call `select_option` with SELECT_OPTION_TIMEOUT_MS so we never burn
       the full 30s default on a stale value.

    Returns True if an option was selected.
    """
    if not field_id:
        return False

    el = page.locator(f'[id="{field_id}"]')
    if not await el.count():
        return False

    real = await _list_real_select_options(page, field_id)
    if not real:
        # Either not a select, or DOM enum failed. Fall back to the legacy
        # call but with the short timeout to avoid hanging.
        if desired_value is None and desired_text is None:
            return False
        try:
            kwargs = {"timeout": SELECT_OPTION_TIMEOUT_MS}
            if desired_value is not None:
                kwargs["value"] = str(desired_value)
            else:
                kwargs["label"] = str(desired_text)
            await el.select_option(**kwargs)
            logger.info(
                f"    Selected option (no DOM enum): "
                f"value={desired_value!r} text={desired_text!r}"
            )
            return True
        except Exception as exc:
            logger.warning(f"    select_option failed without DOM enum: {exc}")
            return False

    real_values = {o.get("value") for o in real}
    chosen = None

    # If caller passed an exact value that exists in the DOM, honor it.
    if desired_value is not None and str(desired_value) in real_values:
        chosen = next((o for o in real if o.get("value") == str(desired_value)), None)
    else:
        # Fuzzy-match using whichever hint the caller supplied.
        target = desired_text if desired_text is not None else (desired_value or "")
        chosen = _pick_best_real_option(real, str(target))

    if not chosen:
        logger.warning(
            f"    No selectable option for #{field_id} "
            f"(wanted value={desired_value!r}, text={desired_text!r})"
        )
        return False

    try:
        await el.select_option(value=chosen.get("value"), timeout=SELECT_OPTION_TIMEOUT_MS)
        logger.info(
            f"    Selected option: value={chosen.get('value')!r} "
            f"text={chosen.get('text', '')!r}"
        )
        return True
    except Exception as exc:
        # Last-ditch: try selecting by label.
        try:
            await el.select_option(label=chosen.get("text"), timeout=SELECT_OPTION_TIMEOUT_MS)
            logger.info(f"    Selected option (by label): {chosen.get('text')!r}")
            return True
        except Exception as exc2:
            logger.warning(
                f"    select_option failed for #{field_id}: "
                f"value-attempt={exc}; label-attempt={exc2}"
            )
            return False


async def _select_best_option(page: Page, field_id: str, options: list, target: str):
    """Select the dropdown option closest to `target`.

    Thin wrapper around `_safe_select_option` retained for callers that still
    pass pre-scraped option dicts. The helper re-reads the live DOM so it
    never tries to select an option that no longer exists (the root cause of
    the 30s `select_option` hang on Seek question_70).
    """
    await _safe_select_option(page, field_id, desired_text=target)


async def _claude_answer(question: str, options: list | None, job: JobListing,
                          model: str | None = None) -> str:
    """Answer an employer screening question via the `claude` CLI."""
    try:
        resume_text = load_profile().strip()
        prompt = (
            f"You are answering a job application screening question on behalf of the candidate.\n"
            f"Candidate visa status: 485 Temporary Graduate visa, expires 8 July 2028. "
            f"NOT an Australian citizen or permanent resident.\n\n"
            f"--- CANDIDATE RESUME ---\n{resume_text}\n--- END RESUME ---\n\n"
            f"Job title: {job.title}\n"
            f"Company: {job.company}\n\n"
            f"Question/field: {question}\n"
        )
        if options:
            prompt += f"Available options: {', '.join(options)}\n"
            prompt += (
                "Reply with ONLY the best option text, nothing else.\n"
                "Candidate facts:\n"
                "- NOT an Australian citizen or permanent resident — never pick those.\n"
                "- Holds 485 Temporary Graduate Visa (also called 'Subclass 485', "
                "'Temporary Graduate Visa', 'Visa with no restrictions', 'Post-study Work').\n"
                "- HAS the right to work in Australia.\n"
                "- Does NOT have any security clearance (no NV1, NV2, baseline, AGSVA).\n"
                "- 'Yes' to right-to-work / authorised-to-work questions.\n"
                "- 'No' to citizenship / PR / security-clearance questions."
            )
        else:
            prompt += (
                "Reply with a SHORT answer (under 20 words) suitable for a job application.\n"
                "Base your answer on the candidate's actual resume above.\n"
                "Write naturally. Avoid AI filler (passionate, leveraging, thrilled, excited, pivotal).\n"
                "Candidate facts:\n"
                "- NOT an Australian citizen or permanent resident.\n"
                "- Holds 485 Temporary Graduate Visa (expires 8 July 2028). "
                "Aliases: 'Subclass 485', 'Temporary Graduate Visa', 'Visa with no restrictions'.\n"
                "- HAS the right to work in Australia (answer 'Yes' to right-to-work questions).\n"
                "- Does NOT have any security clearance (answer 'No' to NV1/NV2/baseline questions).\n"
                "- For salary: answer 'Negotiable'.\n"
                "- For availability / notice period: answer '2 weeks'.\n"
                "Reply with ONLY the answer text."
            )
        text = await claude_complete(
            system="You are answering an employer screening question. Reply with ONLY the answer text — no preamble, no explanation.",
            user=prompt,
            model=DEFAULT_MODEL,
        )
        return text.strip()
    except Exception as e:
        logger.warning(f"    Claude answer failed: {e} — using fallback")
        if options:
            return options[0]
        return "Yes"


# ── Navigation helpers ────────────────────────────────────────────────────────

async def _click_continue(page: Page):
    """Click Continue using JS to bypass any overlay. Fails fast if session expired."""
    await asyncio.sleep(0.5)
    _assert_not_login(page)
    for attempt in range(3):
        try:
            btn = page.get_by_role("button", name="Continue")
            if await btn.count() == 0:
                btn = page.locator("button:has-text('Continue')")
            # Short timeout — fail fast rather than hanging 30s
            await btn.first.scroll_into_view_if_needed(timeout=5000)
            await btn.first.evaluate("el => el.click()")
            logger.info("  Clicked Continue")
            await asyncio.sleep(1)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            _assert_not_login(page)  # check again after navigation
            await asyncio.sleep(1)
            return
        except PermissionError:
            raise
        except Exception as e:
            if attempt == 2:
                raise SeekApplyError(f"Could not click Continue: {e}")
            await asyncio.sleep(1)


async def _tick_terms_checkbox(page: Page):
    """On the review step, Seek may show a 'must accept' / consent checkbox
    that has to be ticked before Submit. Find and tick it."""
    try:
        ticked = await page.evaluate("""() => {
            let n = 0;
            document.querySelectorAll('input[type=checkbox]').forEach(cb => {
                if (!cb.offsetParent || cb.checked) return;
                const lbl = (cb.id && document.querySelector(`label[for="${cb.id}"]`)?.innerText || '').toLowerCase();
                const near = (cb.closest('div')?.innerText || '').toLowerCase().slice(0, 200);
                const txt = lbl + ' ' + near;
                if (/(accept|agree|consent|terms|privacy|i confirm|continue before submit)/.test(txt)) {
                    const lblEl = cb.id ? document.querySelector(`label[for="${cb.id}"]`) : null;
                    (lblEl || cb).click();
                    n++;
                }
            });
            return n;
        }""")
        if ticked:
            logger.info(f"  ✅ Ticked {ticked} 'accept/agree' checkbox(es) before submit")
    except Exception as e:
        logger.warning(f"  Could not auto-tick consent checkbox: {e}")


async def _submit(page: Page):
    """Click Submit application using JS to bypass overlays."""
    await asyncio.sleep(1)
    # Tick any 'must accept' / consent checkbox before clicking Submit
    await _tick_terms_checkbox(page)
    for selector_text in ["Submit application", "Submit", "Apply now", "Apply"]:
        btn = page.get_by_role("button", name=selector_text, exact=False)
        if await btn.count() > 0:
            try:
                await btn.first.scroll_into_view_if_needed()
                await btn.first.evaluate("el => el.click()")
                logger.info(f"  Clicked '{selector_text}'")
                await asyncio.sleep(3)
                return
            except Exception as e:
                logger.warning(f"  Submit click failed for '{selector_text}': {e}")
    raise SeekApplyError("Could not find Submit button")


async def _scrape_applied_cards(page) -> list[dict]:
    """
    Read every applied-job card on /my-activity/applied-jobs and return
    [{title, company}, ...]. Seek's card layout uses labelled spans:
      <span>Job Title </span><actual title>
      <span>Advertiser </span><actual company>
    There is no /job/<id> link on the cards, so title+company is the only signal.
    """
    return await page.evaluate("""() => {
        const cards = [];
        document.querySelectorAll('span').forEach(label => {
            if (label.textContent.trim() !== 'Job Title') return;
            const titleParent = label.parentElement;
            if (!titleParent) return;
            const title = titleParent.textContent.replace(/^\\s*Job Title\\s*/, '').trim();
            // Walk up to the card container (one that also has an 'Advertiser' label)
            let card = titleParent;
            for (let i = 0; i < 8 && card; i++) {
                const advLabel = Array.from(card.querySelectorAll('span'))
                    .find(s => s.textContent.trim() === 'Advertiser');
                if (advLabel) {
                    const advParent = advLabel.parentElement;
                    const company = advParent.textContent.replace(/^\\s*Advertiser\\s*/, '').trim();
                    cards.push({title, company});
                    return;
                }
                card = card.parentElement;
            }
            cards.push({title, company: ''});
        });
        return cards;
    }""")


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace for fuzzy matching."""
    return " ".join((s or "").lower().split())


async def _verify_applied(page: Page, job_title: str, job_company: str) -> bool:
    """
    Navigate to Seek's Applied Jobs page and check whether a card with
    matching title AND company appears. Both must match (within fuzz) to count.
    Retries 3x to cover Seek's lag in rendering the new application.
    """
    target_title = _normalize(job_title)
    target_company = _normalize(job_company)

    for attempt in range(1, 4):
        try:
            await page.goto(
                "https://au.seek.com/my-activity/applied-jobs",
                wait_until="networkidle",
                timeout=25000,
            )
            await asyncio.sleep(3 + attempt)  # let cards render
            cards = await _scrape_applied_cards(page)
            for c in cards:
                ct = _normalize(c["title"])
                cc = _normalize(c["company"])
                title_match = target_title and (
                    target_title in ct or ct in target_title
                )
                company_match = target_company and (
                    target_company in cc or cc in target_company
                )
                if title_match and company_match:
                    return True
            logger.info(
                f"  Verify attempt {attempt}/3: '{job_title} @ {job_company}' "
                f"not yet in {len(cards)} cards; retrying"
            )
        except Exception as e:
            logger.warning(f"  Verify attempt {attempt}/3 errored: {e}")
        await asyncio.sleep(3)
    return False
