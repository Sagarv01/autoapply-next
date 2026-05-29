import asyncio
import json
import random
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright_stealth import Stealth

from models import JobListing

SESSION_DIR = Path("sessions")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class BaseScraper:
    board: str  # set by subclass

    def __init__(self):
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._blocked_until: float = 0.0

    @property
    def session_dir(self) -> Path:
        return SESSION_DIR / self.board

    @staticmethod
    def _clean_state_file(state_file: Path):
        """Strip Chrome-only cookie fields that Playwright rejects on load."""
        if not state_file.exists():
            return
        bad = ("partitionKey", "priority", "sourceScheme", "sourcePort",
               "session", "size", "expirationDate", "storeId")
        raw = json.loads(state_file.read_text())
        for c in raw.get("cookies", []):
            for f in bad:
                c.pop(f, None)
        state_file.write_text(json.dumps(raw))

    async def start(self, playwright):
        # Use a persistent user-data-dir per board so Chrome carries real
        # cookies/history/fingerprint across runs. This is the strongest
        # signal we can give Seek that we're a real browser, not Playwright.
        # The board-specific dir lets Seek and Indeed sessions stay isolated.
        user_data_dir = (SESSION_DIR / f"{self.board}_chrome_profile").resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        launch_kwargs: dict = {
            "user_data_dir": str(user_data_dir),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "user_agent": random.choice(USER_AGENTS),
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800",
            ],
        }
        if Path(chrome_path).exists():
            launch_kwargs["executable_path"] = chrome_path
        self._context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._browser = self._context.browser  # may be None for persistent contexts
        # Apply playwright-stealth to suppress navigator.webdriver + CDP signals
        _stealth = Stealth()
        self._context.on(
            "page",
            lambda page: asyncio.create_task(_stealth.apply_stealth_async(page))
        )

    async def save_session(self):
        # No-op: persistent context auto-saves cookies/storage to user-data-dir.
        return

    async def stop(self):
        if self._context:
            await self._context.close()
        # _browser is owned by the persistent context; closing context closes it.

    def mark_blocked(self, pause_minutes: int = 15):
        self._blocked_until = time.time() + pause_minutes * 60

    def is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    async def human_delay(self, min_s: float = 1.0, max_s: float = 4.0):
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def scrape(self, skills: list[str]) -> list[JobListing]:
        raise NotImplementedError
