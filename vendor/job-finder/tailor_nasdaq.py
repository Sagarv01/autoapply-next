"""One-shot: tailor resume + cover letter for the Nasdaq DevOps Engineer role.
Loads env BEFORE importing tailorer (which constructs the Anthropic client at
module-import time)."""
import asyncio
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY missing in .env"

from playwright.async_api import async_playwright
from models import JobListing
from tailorer import tailor


URL = (
    "https://nasdaq.wd1.myworkdayjobs.com/Global_External_Site/job/"
    "Australia---Sydney---New-South-Wales/"
    "DevOps-Engineer---NMS-SaaS--Operations--Deployment---Automation-Enablement-_R0025490"
)


async def fetch_workday_description(url: str) -> str:
    """Open the Workday job page in an ephemeral browser context and grab the body text."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        # Workday wraps job content in [data-automation-id="jobPostingDescription"]
        text = ""
        for sel in (
            '[data-automation-id="jobPostingDescription"]',
            'article',
            'main',
        ):
            el = page.locator(sel)
            if await el.count():
                text = (await el.first.inner_text()).strip()
                break
        if not text:
            text = (await page.locator("body").inner_text()).strip()
        await browser.close()
        return text[:6000]


async def main():
    print(f"Fetching description from: {URL}")
    desc = await fetch_workday_description(URL)
    print(f"Got {len(desc)} chars of description.\n")
    Path("output").mkdir(exist_ok=True)
    Path("output/nasdaq_jd.txt").write_text(desc)
    print("Saved description to output/nasdaq_jd.txt for reference.")

    job = JobListing(
        url=URL,
        title="DevOps Engineer – NMS SaaS (Operations, Deployment & Automation Enablement)",
        company="Nasdaq",
        board="recruiter-direct",
        description=desc,
        easy_apply=False,
    )

    print(f"\nTailoring full-tier (Sonnet) for: {job.title} @ {job.company}\n")
    resume_pdf, cover_pdf = await tailor(job, tier="full")

    # Copy outputs to ~/Downloads
    import shutil
    downloads = Path.home() / "Downloads"
    final_resume = downloads / Path(resume_pdf).name
    final_cover = downloads / Path(cover_pdf).name
    shutil.copy(resume_pdf, final_resume)
    shutil.copy(cover_pdf, final_cover)
    print(f"\n✅ Resume:       {final_resume}")
    print(f"✅ Cover letter: {final_cover}")


if __name__ == "__main__":
    asyncio.run(main())
