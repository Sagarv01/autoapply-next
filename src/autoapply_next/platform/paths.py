"""Cross-platform user-scoped path resolution for AutoApply Next.

Resolves the canonical OS-specific locations for application data, cache, and
logs, and locates the Chrome executable. All directory-returning helpers create
their directory on first call so callers can write straight away.

Overrideable via env vars:

- ``AUTOAPPLY_NEXT_DATA_DIR``      : override for :func:`app_data_dir`
- ``AUTOAPPLY_NEXT_CACHE_DIR``     : override for :func:`app_cache_dir`
- ``AUTOAPPLY_NEXT_LOG_DIR``       : override for :func:`app_log_dir`
- ``AUTOAPPLY_NEXT_ENGINE_WORKDIR``: override for :func:`engine_workdir`

These are mostly so developers can point a dev build at a throwaway directory
without disturbing the production install on the same machine.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "AutoApply Next"


def _home() -> Path:
    """Resolve the user's home directory as an absolute Path."""

    return Path(os.path.expanduser("~")).resolve()


def _env_override(var: str) -> Path | None:
    """Return an absolute Path from ``$var`` if it is set and non-empty."""

    val = os.environ.get(var, "").strip()
    if not val:
        return None
    return Path(val).expanduser().resolve()


def _ensure(p: Path) -> Path:
    """Create ``p`` (and parents) if missing and return it as absolute."""

    p = p.expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def app_data_dir() -> Path:
    """User-scoped app data directory.

    macOS: ``~/Library/Application Support/AutoApply Next``
    Windows: ``%APPDATA%\\AutoApply Next`` (i.e.
    ``C:\\Users\\<user>\\AppData\\Roaming\\AutoApply Next``)
    Linux: ``$XDG_DATA_HOME/AutoApply Next`` or
    ``~/.local/share/AutoApply Next``

    Creates the directory if missing.
    """

    override = _env_override("AUTOAPPLY_NEXT_DATA_DIR")
    if override is not None:
        return _ensure(override)

    if sys.platform == "darwin":
        base = _home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            base = Path(appdata) / APP_NAME
        else:
            base = _home() / "AppData" / "Roaming" / APP_NAME
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg:
            base = Path(xdg) / APP_NAME
        else:
            base = _home() / ".local" / "share" / APP_NAME

    return _ensure(base)


def app_cache_dir() -> Path:
    """User-scoped app cache directory (for things you can lose).

    macOS: ``~/Library/Caches/AutoApply Next``
    Windows: ``%LOCALAPPDATA%\\AutoApply Next\\Cache``
    Linux: ``$XDG_CACHE_HOME/AutoApply Next`` or
    ``~/.cache/AutoApply Next``

    Creates the directory if missing.
    """

    override = _env_override("AUTOAPPLY_NEXT_CACHE_DIR")
    if override is not None:
        return _ensure(override)

    if sys.platform == "darwin":
        base = _home() / "Library" / "Caches" / APP_NAME
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            base = Path(local) / APP_NAME / "Cache"
        else:
            base = _home() / "AppData" / "Local" / APP_NAME / "Cache"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg:
            base = Path(xdg) / APP_NAME
        else:
            base = _home() / ".cache" / APP_NAME

    return _ensure(base)


def app_log_dir() -> Path:
    """User-scoped log directory.

    macOS: ``~/Library/Logs/AutoApply Next``
    Windows: ``%LOCALAPPDATA%\\AutoApply Next\\Logs``
    Linux: ``$XDG_STATE_HOME/AutoApply Next/Logs`` or
    ``~/.local/state/AutoApply Next/Logs``

    This is where ``bot.log`` and friends end up. Creates the directory if
    missing.
    """

    override = _env_override("AUTOAPPLY_NEXT_LOG_DIR")
    if override is not None:
        return _ensure(override)

    if sys.platform == "darwin":
        base = _home() / "Library" / "Logs" / APP_NAME
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            base = Path(local) / APP_NAME / "Logs"
        else:
            base = _home() / "AppData" / "Local" / APP_NAME / "Logs"
    else:
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        if xdg:
            base = Path(xdg) / APP_NAME / "Logs"
        else:
            base = _home() / ".local" / "state" / APP_NAME / "Logs"

    return _ensure(base)


def engine_workdir() -> Path:
    """The workdir to pass to the engine adapter.

    Lives under ``app_data_dir()/engine/``. This is where the engine's
    ``sessions/``, ``assets/``, ``config.yaml``, ``output/``, ``jobs.db``,
    ``errors/`` all live in production. For development, the user can point
    at a different directory via the ``AUTOAPPLY_NEXT_ENGINE_WORKDIR`` env
    var.
    """

    override = _env_override("AUTOAPPLY_NEXT_ENGINE_WORKDIR")
    if override is not None:
        return _ensure(override)

    return _ensure(app_data_dir() / "engine")


def locate_chrome() -> Path | None:
    """Locate Google Chrome's executable.

    Returns the Path if found, ``None`` otherwise. Tries platform-specific
    canonical install locations first, then falls back to ``shutil.which``
    for the common command names.

    macOS: ``/Applications/Google Chrome.app/Contents/MacOS/Google Chrome``
    Windows: ``%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe``
    (and ``%PROGRAMFILES(X86)%`` and ``%LOCALAPPDATA%`` variants)
    Linux: ``shutil.which('google-chrome')`` or ``shutil.which('chromium')``
    """

    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            _home()
            / "Applications"
            / "Google Chrome.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome",
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return p.resolve()
        for name in ("google-chrome", "chromium", "chrome"):
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
        return None

    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get(
            "PROGRAMFILES(X86)", r"C:\Program Files (x86)"
        )
        local_appdata = os.environ.get(
            "LOCALAPPDATA", str(_home() / "AppData" / "Local")
        )
        candidates = [
            Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(program_files_x86)
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe",
            Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return p.resolve()
        for name in ("chrome.exe", "chrome"):
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
        return None

    # Linux and any other POSIX target.
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    # Last-ditch: common Debian/Fedora install locations.
    for p in (
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/snap/bin/chromium"),
    ):
        if p.exists() and p.is_file():
            return p.resolve()
    return None


if __name__ == "__main__":
    print(f"platform        : {sys.platform}")
    print(f"app_data_dir()  : {app_data_dir()}")
    print(f"app_cache_dir() : {app_cache_dir()}")
    print(f"app_log_dir()   : {app_log_dir()}")
    print(f"engine_workdir(): {engine_workdir()}")
    print(f"locate_chrome() : {locate_chrome()}")
