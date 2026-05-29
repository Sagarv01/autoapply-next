import asyncio
import logging
import tracker
from models import JobListing
from scraper import BaseScraper

logger = logging.getLogger(__name__)

LOGIN_URL = "https://secure.indeed.com/account/login"
SEARCH_URL = "https://au.indeed.com/jobs"


class IndeedScraper(BaseScraper):
    board = "indeed"

    def _filter_seen(self, jobs: list[dict]) -> list[dict]:
        return [j for j in jobs if not tracker.is_seen(j["url"])]

    async def _ensure_logged_in(self):
        """Check session is valid. If not, raise error telling user to run setup_sessions.py."""
        state_file = self.session_dir / "state.json"
        if not state_file.exists():
            raise RuntimeError(
                "No Indeed session found. Run setup_sessions.py first to log in manually:\n"
                "  python setup_sessions.py"
            )
        page = await self._context.new_page()
        try:
            await page.goto("https://au.indeed.com", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            if "login" in page.url.lower() or "signin" in page.url.lower():
                raise RuntimeError(
                    "Indeed session expired. Run setup_sessions.py to log in again:\n"
                    "  python setup_sessions.py"
                )
        finally:
            await page.close()

    async def _scrape_skill(self, skill: str, location: str) -> list[dict]:
        page = await self._context.new_page()
        jobs = []
        try:
            params = f"?q={skill.replace(' ', '+')}&l={location}&sort=date&fromage=1"
            await page.goto(SEARCH_URL + params, wait_until="domcontentloaded")
            await self.human_delay(2, 4)
            cards = await page.query_selector_all('[data-jk]')
            for card in cards[:20]:
                try:
                    jk = await card.get_attribute("data-jk")
                    if not jk:
                        continue
                    url = f"https://au.indeed.com/viewjob?jk={jk}"
                    title_el = await card.query_selector('[data-testid="job-title"]')
                    company_el = await card.query_selector('[data-testid="company-name"]')
                    date_el = await card.query_selector('[data-testid="myJobsStateDate"]')
                    title = (await title_el.inner_text()).strip() if title_el else ""
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    posted = (await date_el.inner_text()).strip() if date_el else ""
                    desc = await self._fetch_description(url)
                    jobs.append({"url": url, "title": title, "company": company,
                                 "description": desc, "posted_at": posted})
                    await self.human_delay(0.5, 1.5)
                except Exception as e:
                    logger.warning(f"Indeed card parse error: {e}")
        finally:
            await page.close()
        return jobs

    async def _fetch_description(self, url: str) -> str:
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self.human_delay(1, 2)
            el = await page.query_selector('[id="jobDescriptionText"]')
            return (await el.inner_text()).strip() if el else ""
        finally:
            await page.close()

    async def scrape(self, skills: list[str], location: str = "Australia") -> list[JobListing]:
        if self.is_blocked():
            return []
        await self._ensure_logged_in()
        all_jobs: list[dict] = []
        seen_urls: set = set()
        for skill in skills:
            try:
                raw = await self._scrape_skill(skill, location)
                for j in raw:
                    if j["url"] not in seen_urls:
                        seen_urls.add(j["url"])
                        all_jobs.append(j)
                await self.human_delay(2, 5)
            except Exception as e:
                logger.error(f"Indeed skill '{skill}' scrape failed: {e}")
        new_jobs = self._filter_seen(all_jobs)
        for j in new_jobs:
            tracker.mark_seen(j["url"])
        return [
            JobListing(url=j["url"], title=j["title"], company=j["company"],
                       board="indeed", description=j["description"], posted_at=j.get("posted_at", ""))
            for j in new_jobs
        ]
