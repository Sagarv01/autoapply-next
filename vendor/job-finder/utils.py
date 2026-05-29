import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar('T')

_PROFILE_PATH = Path(__file__).parent / "assets" / "profile.txt"
_profile_cache: str | None = None


def load_profile() -> str:
    """Read assets/profile.txt once per process and cache."""
    global _profile_cache
    if _profile_cache is None:
        _profile_cache = _PROFILE_PATH.read_text(encoding="utf-8")
    return _profile_cache


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating ```fences and prose preambles."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", body, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def sanitise_name(text: str) -> str:
    """'DevOps Engineer (AWS)' -> 'DevOpsEngineerAWS'"""
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    # Capitalise first letter of each word but preserve existing capitalisation
    words = [w[0].upper() + w[1:] if w else w for w in cleaned.split()]
    return ''.join(words)


def make_filename(prefix: str, company: str, title: str) -> str:
    """SagarVerma_Atlassian_DevOpsEngineer_20260401_143211_042.pdf"""
    now = datetime.now()
    ts = now.strftime('%Y%m%d_%H%M%S_') + f"{now.microsecond // 1000:03d}"
    return f"{prefix}_{sanitise_name(company)}_{sanitise_name(title)}_{ts}.pdf"


def detect_libreoffice() -> str:
    candidates = [
        '/Applications/LibreOffice.app/Contents/MacOS/soffice',
        'libreoffice',
        'soffice',
    ]
    for c in candidates:
        if Path(c).exists() or shutil.which(c):
            return c
    raise RuntimeError(
        "LibreOffice not found.\n"
        "  macOS: brew install --cask libreoffice\n"
        "  Linux: sudo apt install libreoffice"
    )


async def retry_async(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> T:
    """Exponential backoff retry. coro_fn is a zero-argument async callable."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc
