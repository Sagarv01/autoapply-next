"""
Re-apply to the VTAC Infrastructure & Security Engineer job using the patched
checkbox handler. Reuses the existing tailored resume + cover letter PDFs.
"""
import asyncio
import logging
import os
import yaml
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

import seek_apply
import tracker
from models import Application, JobListing

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("vtac")

JOB_URL = "https://au.seek.com/job/91547118"
APPLY_URL = JOB_URL + "/apply"
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
RESUME = Path("output/SagarVerma_VictorianTertiaryAdmissionsCentreLimitedVTAC_InfrastructureAndSecurityEngineer_20260417_111209_173.pdf").resolve()
COVER = Path("output/CoverLetter_VictorianTertiaryAdmissionsCentreLimitedVTAC_InfrastructureAndSecurityEngineer_20260417_111211_796.pdf").resolve()


async def main():
    cfg = yaml.safe_load(open("config.yaml"))
    candidate = cfg["candidate"]

    job = JobListing(
        url=JOB_URL,
        title="Infrastructure and Security Engineer",
        company="Victorian Tertiary Admissions Centre Limited (VTAC)",
        board="seek",
        description="",
        easy_apply=True,
    )

    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()

        log.info(f"Opening {APPLY_URL}")
        await page.goto(APPLY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        body = (await page.content()).lower()
        if "you've applied" in body or "already applied" in body or "view application" in body:
            log.warning("⚠️  Already applied — exiting.")
            await browser.close()
            return

        try:
            status = await seek_apply.apply_seek_quick(
                job=job,
                resume_pdf=str(RESUME),
                cover_pdf=str(COVER),
                candidate=candidate,
                session_state=SESSION,
                page=page,
            )
            log.info(f"Result: {status}")

            tracker.init_db()
            app = Application(
                url=job.url, title=job.title, company=job.company, board=job.board,
                status=status, resume_file=RESUME.name, cover_letter_file=COVER.name,
                notes="Re-applied via apply_vtac.py after checkbox-handler patch",
            )
            tracker.upsert_application(app)
            log.info("Tracker updated.")
        except Exception as e:
            log.error(f"Apply failed: {e}", exc_info=True)
        finally:
            await asyncio.sleep(5)
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
