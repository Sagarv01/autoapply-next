import asyncio
import logging
from typing import Any

import tracker
from models import JobListing
from scraper import BaseScraper

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.linkedin.com/login"
JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"


class LinkedInScraper(BaseScraper):
    board = "linkedin"

    def _deduplicate(self, jobs: list[dict], seen_urls: set) -> list[dict]:
        result = []
        for j in jobs:
            if j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                result.append(j)
        return result

    def _filter_seen(self, jobs: list[dict]) -> list[dict]:
        return [j for j in jobs if not tracker.is_seen(j["url"])]

    async def _ensure_logged_in(self, email: str, password: str):
        page = await self._context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
            await self.human_delay()
            # Check if already logged in
            if "feed" in page.url or "mynetwork" in page.url or "jobs" in page.url:
                return
            if await page.query_selector('[data-test-id="nav-top"]'):
                return
            # Check if login form is available (not blocked by CAPTCHA/bot detection)
            username_field = await page.query_selector("#username")
            if not username_field:
                raise PermissionError(
                    "LinkedIn login blocked (bot detection / CAPTCHA). "
                    "Re-run setup_sessions.py with a visible browser to refresh the session."
                )
            await page.fill("#username", email, timeout=10000)
            await self.human_delay(0.5, 1.5)
            await page.fill("#password", password, timeout=10000)
            await self.human_delay(0.5, 1.5)
            await page.click('[type="submit"]')
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            await self.human_delay()
            if "checkpoint" in page.url or "challenge" in page.url:
                raise PermissionError("LinkedIn requires CAPTCHA verification — re-run setup_sessions.py")
            if "login" in page.url:
                raise PermissionError("LinkedIn login failed — check credentials in .env")
            await self.save_session()
        finally:
            await page.close()

    async def _scrape_skill(self, skill: str, location: str) -> list[dict]:
        page = await self._context.new_page()
        jobs = []
        try:
            params = f"?keywords={skill.replace(' ', '%20')}&location={location}&sortBy=DD"
            await page.goto(JOBS_SEARCH_URL + params, wait_until="domcontentloaded")
            await self.human_delay(2, 4)
            for _ in range(3):
                await page.keyboard.press("End")
                await self.human_delay(1, 2)
            cards = await page.query_selector_all(".job-search-card")
            for card in cards[:20]:
                try:
                    url_el = await card.query_selector("a.base-card__full-link")
                    title_el = await card.query_selector(".base-search-card__title")
                    company_el = await card.query_selector(".base-search-card__subtitle")
                    time_el = await card.query_selector("time")
                    if not url_el:
                        continue
                    url = await url_el.get_attribute("href")
                    title = (await title_el.inner_text()).strip() if title_el else ""
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    posted = await time_el.get_attribute("datetime") if time_el else ""
                    desc = await self._fetch_description(url)
                    jobs.append({
                        "url": url.split("?")[0],
                        "title": title,
                        "company": company,
                        "description": desc,
                        "posted_at": posted,
                    })
                    await self.human_delay(0.5, 1.5)
                except Exception as e:
                    logger.warning(f"LinkedIn card parse error: {e}")
        finally:
            await page.close()
        return jobs

    async def _fetch_description(self, url: str) -> str:
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self.human_delay(1, 2)
            el = await page.query_selector(".show-more-less-html__markup")
            return (await el.inner_text()).strip() if el else ""
        finally:
            await page.close()

    async def scrape(self, skills: list[str], email: str, password: str,
                     location: str = "Australia") -> list[JobListing]:
        if self.is_blocked():
            return []
        await self._ensure_logged_in(email, password)
        all_jobs: list[dict] = []
        seen_urls: set = set()
        for skill in skills:
            try:
                raw = await self._scrape_skill(skill, location)
                deduped = self._deduplicate(raw, seen_urls)
                all_jobs.extend(deduped)
                await self.human_delay(2, 5)
            except Exception as e:
                logger.error(f"LinkedIn skill '{skill}' scrape failed: {e}")
        new_jobs = self._filter_seen(all_jobs)
        logger.info(f"LinkedIn: {len(all_jobs)} total jobs → {len(new_jobs)} new")
        for j in new_jobs:
            tracker.mark_seen(j["url"])
        return [
            JobListing(
                url=j["url"], title=j["title"], company=j["company"],
                board="linkedin", description=j["description"], posted_at=j.get("posted_at", ""),
            )
            for j in new_jobs
        ]
