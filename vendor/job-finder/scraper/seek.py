import asyncio
import logging
import tracker
from models import JobListing
from scraper import BaseScraper

logger = logging.getLogger(__name__)

LOGIN_URL = "https://au.seek.com/oauth/login"
SEARCH_URL = "https://au.seek.com/jobs"


class SeekScraper(BaseScraper):
    board = "seek"

    def _filter_seen(self, jobs: list[dict]) -> list[dict]:
        # Drop jobs already applied / permanently skipped, AND jobs that have
        # failed >= PERMANENT_FAILURE_THRESHOLD times (structural failure, not
        # transient — usually an unanswerable employer question or a broken
        # form). Without the failure_count filter the same broken job got
        # rescraped + re-tried every cycle, burning Claude API calls.
        permafail = tracker.permanently_failed_urls()
        return [
            j for j in jobs
            if not tracker.is_applied_or_skipped(j["url"])
            and j["url"] not in permafail
        ]

    async def _ensure_logged_in(self):
        """Check session is valid. Seek jobs are publicly viewable so scraping works either way,
        but we warn if not logged in since applying will fail."""
        # Session lives in sessions/seek_chrome_profile/ (persistent Chrome user-data-dir).
        # If empty, Seek redirects to /oauth/login. Run setup_chrome_profile.py to log in.
        page = await self._context.new_page()
        try:
            await page.goto("https://au.seek.com", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            url = page.url.lower()
            if "login" in url or "oauth" in url:
                logger.warning(
                    "Seek session expired — scraping public listings only (cannot apply). "
                    "Re-run setup_chrome_profile.py to log in again."
                )
                return
            # Check for logged-in indicators — wait for React hydration before checking
            await asyncio.sleep(3)
            content = (await page.content()).lower()
            logged_in_signals = ["sign out", "signout", "my profile", "my account",
                                 "logged in", "dashboard", "/profile/"]
            if not any(s in content for s in logged_in_signals):
                logger.warning(
                    "Seek not logged in — scraping public listings only (cannot apply). "
                    "Re-run setup_chrome_profile.py to log in again."
                )
        finally:
            await page.close()

    # Seek caps search results at 17 pages — page 18 onwards always returns
    # an empty result set regardless of keywords. Anything past 17 is wasted
    # page loads and risk against the anti-automation flag.
    async def _scrape_skill(self, skill: str, location: str, max_pages: int = 17,
                            sort_by_date: bool = True) -> list[dict]:
        jobs = []
        keyword = skill.replace(' ', '+')
        consecutive_empty = 0
        for page_num in range(1, max_pages + 1):
            page = await self._context.new_page()
            try:
                sort = "&sortmode=ListedDate" if sort_by_date else ""
                params = (f"?keywords={keyword}&where={location}"
                          f"{sort}&page={page_num}")
                await page.goto(SEARCH_URL + params, wait_until="domcontentloaded")
                await self.human_delay(2, 4)
                cards = await page.query_selector_all('[data-automation="normalJob"]')
                if not cards:
                    consecutive_empty += 1
                    logger.warning(f"Seek: '{skill}' page {page_num} → 0 cards (empty {consecutive_empty}/3)")
                    if consecutive_empty >= 3:
                        logger.info(f"Seek: '{skill}' — 3 consecutive empty pages, stopping")
                        break
                    await self.human_delay(3, 5)
                    continue
                consecutive_empty = 0
                for card in cards:
                    try:
                        link = await card.query_selector('a[data-automation="jobTitle"]')
                        company_el = await card.query_selector('[data-automation="jobCompany"]')
                        date_el = await card.query_selector('[data-automation="jobListingDate"]')
                        if not link:
                            continue
                        href = await link.get_attribute("href")
                        url = f"https://au.seek.com{href}".split("?")[0]
                        title = (await link.inner_text()).strip()
                        company = (await company_el.inner_text()).strip() if company_el else ""
                        posted = (await date_el.inner_text()).strip() if date_el else ""
                        jobs.append({"url": url, "title": title, "company": company,
                                     "description": "", "posted_at": posted,
                                     "easy_apply": True})
                    except Exception as e:
                        logger.warning(f"Seek card parse error: {e}")
                logger.info(f"Seek: '{skill}' page {page_num} → {len(cards)} cards")
                await self.human_delay(2, 4)
            except Exception as e:
                logger.warning(f"Seek: '{skill}' page {page_num} failed ({e}) — skipping")
                await self.human_delay(3, 5)
            finally:
                await page.close()
        return jobs

    async def scrape(self, skills: list[str], location: str = "Australia",
                     sort_by_date: bool = True,
                     max_jobs: int | None = None) -> list[JobListing]:
        if self.is_blocked():
            return []
        await self._ensure_logged_in()
        logger.info(f"Seek: starting scrape for {len(skills)} skills")
        all_jobs: list[dict] = []
        seen_urls: set = set()
        for skill in skills:
            try:
                raw = await self._scrape_skill(skill, location, sort_by_date=sort_by_date)
                logger.info(f"Seek: '{skill}' → {len(raw)} total jobs")
                for j in raw:
                    if max_jobs is not None and len(all_jobs) >= max_jobs:
                        break
                    if j["url"] not in seen_urls:
                        seen_urls.add(j["url"])
                        all_jobs.append(j)
                if max_jobs is not None and len(all_jobs) >= max_jobs:
                    logger.info(f"Seek: hit scrape cap of {max_jobs} jobs, stopping")
                    break
                await self.human_delay(2, 5)
            except Exception as e:
                logger.error(f"Seek skill '{skill}' scrape failed: {e}")
        new_jobs = self._filter_seen(all_jobs)
        for j in new_jobs:
            tracker.mark_seen(j["url"])
        return [
            JobListing(url=j["url"], title=j["title"], company=j["company"],
                       board="seek", description=j["description"],
                       posted_at=j.get("posted_at", ""),
                       easy_apply=j.get("easy_apply", False))
            for j in new_jobs
        ]
