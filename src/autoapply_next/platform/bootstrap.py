"""Clean-machine bootstrap.

Three concerns that only bite once the app leaves the developer's checkout:

1. ``engine_vendor_dir`` / ``ensure_engine_importable``: locate the vendored
   job-finder engine and put it on ``sys.path`` so ``import seek_apply`` etc.
   resolve in BOTH a dev checkout and a frozen PyInstaller bundle. In a dev
   tree the engine is ``<repo>/vendor/job-finder``; in a frozen build
   PyInstaller unpacks bundled data under ``sys._MEIPASS``. Without the frozen
   branch the packaged app raises ``ModuleNotFoundError`` on the first
   score/tailor/apply.

2. ``seed_engine_workdir``: the production engine workdir resolves to an empty
   per-user directory on a clean machine, and the adapter refuses to start
   without ``config.yaml``. This copies the files the engine needs (config
   template + assets) into an empty workdir so a first run is possible. It is
   also how the live-pilot workdir is seeded from a known-good source config.

3. ``preflight_report``: the engine shells out to the ``claude`` CLI and
   LibreOffice ``soffice``, and drives Playwright Chromium, none of which are
   bundled. This reports which are missing so the UI can block with install
   instructions instead of failing mid-apply.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def engine_vendor_dir() -> Path:
    """Absolute path to the vendored job-finder engine in dev and frozen builds.

    Dev: ``<repo>/vendor/job-finder`` (four parents up from this file:
    platform -> autoapply_next -> src -> repo).
    Frozen: ``<_MEIPASS>/vendor/job-finder`` where PyInstaller unpacks datas.
    """
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "vendor" / "job-finder"
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "vendor"
        / "job-finder"
    )


def ensure_engine_importable() -> Path:
    """Put the vendored engine on ``sys.path`` (idempotent). Returns the dir.

    Logs an ERROR if the dir is missing so a broken bundle is loud rather than
    surfacing later as an opaque ``ModuleNotFoundError`` mid-apply.
    """
    vendor = engine_vendor_dir()
    if vendor.is_dir():
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
            logger.info("engine on sys.path: %s", vendor)
    else:
        logger.error(
            "vendored engine directory not found at %s; the apply pipeline "
            "will fail to import (frozen-build packaging bug or broken "
            "checkout)",
            vendor,
        )
    return vendor


def seed_engine_workdir(
    workdir: Path,
    *,
    config_source: Path | None = None,
    assets_source: Path | None = None,
    force_config: bool = False,
) -> list[str]:
    """Make ``workdir`` runnable: copy in the files the engine requires if
    they are missing, and create the runtime subdirectories.

    Returns a list of human-readable actions taken (empty if nothing needed
    doing).

    ``config.yaml`` is NEVER overwritten unless ``force_config=True``: it may
    hold the user's real candidate facts, and clobbering them would corrupt
    live applications. When seeding, the source is ``config_source`` if given,
    else the vendored ``config.yaml.example`` (preferred, it carries all
    blocks) or ``config.yaml`` as a fallback.

    Assets (``profile.txt`` and the resume template) are copied for any file
    missing from ``workdir/assets``.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    vendor = engine_vendor_dir()
    actions: list[str] = []

    cfg = workdir / "config.yaml"
    if force_config or not cfg.exists():
        src = Path(config_source) if config_source else None
        if src is None or not src.exists():
            for candidate in (vendor / "config.yaml.example", vendor / "config.yaml"):
                if candidate.exists():
                    src = candidate
                    break
        if src and src.exists():
            shutil.copy2(src, cfg)
            actions.append(f"seeded config.yaml from {src}")
        else:
            actions.append(
                "WARNING: no config template found to seed config.yaml"
            )

    src_assets = Path(assets_source) if assets_source else (vendor / "assets")
    if src_assets.is_dir():
        dest_assets = workdir / "assets"
        dest_assets.mkdir(parents=True, exist_ok=True)
        for item in src_assets.iterdir():
            if item.is_file() and not (dest_assets / item.name).exists():
                shutil.copy2(item, dest_assets / item.name)
                actions.append(f"seeded assets/{item.name}")

    for sub in ("sessions/seek", "output/dryrun-screenshots", "errors"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)

    # Create jobs.db's schema if absent. A fresh workdir otherwise has a 0-byte
    # jobs.db with no `applications` table, and the first scrape/apply fails with
    # 'no such table: applications'. (The vendored tracker.init_db that normally
    # creates it is never called by the desktop app.)
    from ..engine.persistence import ensure_jobs_db_schema

    db_path = workdir / "jobs.db"
    needs_schema = True
    if db_path.exists() and db_path.stat().st_size > 0:
        try:
            import sqlite3

            with sqlite3.connect(db_path) as conn:
                needs_schema = not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'"
                ).fetchone()
        except Exception:  # noqa: BLE001 - re-create on any read trouble
            needs_schema = True
    ensure_jobs_db_schema(workdir)
    if needs_schema:
        actions.append("initialised jobs.db schema (applications table)")

    return actions


def workdir_is_runnable(workdir: Path) -> bool:
    """True if the engine adapter will accept this workdir (config.yaml
    present). Mirrors the adapter's EngineNotReadyError precondition."""
    return (Path(workdir) / "config.yaml").exists()


def preflight_report() -> list[str]:
    """Return human-readable problems that would break a real run on this
    machine. Empty list means the unbundled dependencies are all present.

    Checks the two hard binary dependencies the engine shells out to. The
    messages double as install instructions for a clean-machine user.
    """
    problems: list[str] = []
    if shutil.which("claude") is None:
        problems.append(
            "Claude CLI ('claude') is not on PATH. The match and tailor "
            "stages need it. Install with: npm i -g @anthropic-ai/claude-code, "
            "then run 'claude login' once."
        )
    if (
        shutil.which("soffice") is None
        and not Path("/Applications/LibreOffice.app").exists()
    ):
        problems.append(
            "LibreOffice ('soffice') was not found. The tailor stage converts "
            "the resume docx to pdf with it. Install with: "
            "brew install --cask libreoffice (macOS)."
        )
    return problems
