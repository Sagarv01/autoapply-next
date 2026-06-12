# PyInstaller spec for AutoApply Next: builds PySide6 GUI + vendored job-finder engine into a single onedir bundle (macOS .app / Windows dir+exe).
# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# PyInstaller invokes this spec from the project root (where the user runs
# `pyinstaller packaging/pyinstaller.spec`). SPECPATH is provided by PyInstaller
# and points at packaging/, so the project root is one level up.
PROJECT_ROOT = Path(SPECPATH).parent.resolve()
SRC_ROOT = PROJECT_ROOT / "src"
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "job-finder"
ENTRY_SCRIPT = SRC_ROOT / "autoapply_next" / "__main__.py"
RESOURCES_DIR = PROJECT_ROOT / "resources"

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "AutoApply Next"
# PyInstaller wants the EXE / COLLECT "name" to be a filename-safe token.
# We keep spaces in the BUNDLE display name but strip them everywhere else.
BIN_NAME = "AutoApplyNext"

# ---------------------------------------------------------------------------
# Datas: ship the vendored engine tree as-is so its relative file references
# (config.yaml.template, assets/, docs/, etc.) resolve at runtime.
# ---------------------------------------------------------------------------
datas = []

# vendor/job-finder -> bundled as "vendor/job-finder" inside the app payload.
# We walk the tree manually instead of a glob so we can skip __pycache__ and
# any local development artefacts that shouldn't ship.
SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".venv", "node_modules", ".git",
    # Runtime / secret-bearing dirs that must never ship to users.
    "sessions", "output", ".worktrees", ".superpowers", ".claude",
}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _skip_vendor_file(fname):
    """Keep dev secrets and runtime data out of the shipped bundle.

    Excludes any .env* (real creds + backups + the example template), the dev
    config.yaml (carries the developer's candidate facts; config.yaml.example
    is the template that ships and seeds a fresh workdir), local sqlite dbs and
    their WAL/SHM sidecars, logs, and OS cruft. Without this the spec shipped
    vendor/.env (LinkedIn/Seek/Gmail credential keys) to every user.
    """
    if Path(fname).suffix in SKIP_SUFFIXES:
        return True
    if fname.startswith(".env"):
        return True
    if fname in {".DS_Store", "config.yaml"}:
        return True
    if fname.endswith((".log", ".db", "-wal", "-shm")) or ".db." in fname:
        return True
    return False


for dirpath, dirnames, filenames in os.walk(VENDOR_ROOT):
    # Prune skipped directories in-place so os.walk doesn't descend into them.
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fname in filenames:
        if _skip_vendor_file(fname):
            continue
        abs_src = Path(dirpath) / fname
        # Destination is relative path inside the bundle, anchored at the
        # vendor root so layout is preserved.
        rel_dst_dir = Path("vendor") / "job-finder" / Path(dirpath).relative_to(VENDOR_ROOT)
        datas.append((str(abs_src), str(rel_dst_dir)))

# Bundle any resources/ assets (icons, qss, etc.) if the directory exists.
if RESOURCES_DIR.exists():
    for dirpath, dirnames, filenames in os.walk(RESOURCES_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if Path(fname).suffix in SKIP_SUFFIXES:
                continue
            abs_src = Path(dirpath) / fname
            rel_dst_dir = Path("resources") / Path(dirpath).relative_to(RESOURCES_DIR)
            datas.append((str(abs_src), str(rel_dst_dir)))

# PySide6 data files (Qt plugins, translations) are usually picked up by
# PyInstaller's bundled hook, but we explicitly collect to be safe.
datas += collect_data_files("PySide6", includes=["plugins/*", "translations/*"])

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# PySide6 core modules. PyInstaller's hook usually finds these via the import
# graph, but listing them defends against entry scripts that lazy-import.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
]

# Vendored engine modules. These are imported dynamically by the engine
# adapter and would otherwise be missed by PyInstaller's static analyzer.
engine_modules = [
    "seek_apply",
    "applicator",
    "matcher",
    "tailorer",
    "tracker",
    "models",
    "claude_cli",
    "utils",
    "scraper",
    "process_lock",
]
hiddenimports += engine_modules

# Pull in submodules for engine packages and our own package.
hiddenimports += collect_submodules("autoapply_next")

# ---------------------------------------------------------------------------
# Excludes: trim modules we know we don't ship.
# ---------------------------------------------------------------------------
excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "pytest",
    "pytest_qt",
    "pytest_asyncio",
    "IPython",
    "jupyter",
    "notebook",
    "nbconvert",
    "nbformat",
    "PIL.ImageTk",
    "test",
    "tests",
]

# ---------------------------------------------------------------------------
# Analysis -> PYZ -> EXE -> COLLECT -> BUNDLE
# ---------------------------------------------------------------------------
# Make src/ importable during analysis so `import autoapply_next` works.
analysis_pathex = [str(SRC_ROOT), str(VENDOR_ROOT)]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=analysis_pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# Platform-specific icon resolution.
# ---------------------------------------------------------------------------
icon_path = None
if IS_MACOS:
    mac_icon = RESOURCES_DIR / "icon.icns"
    if mac_icon.exists():
        icon_path = str(mac_icon)
elif IS_WINDOWS:
    win_icon = RESOURCES_DIR / "icon.ico"
    if win_icon.exists():
        icon_path = str(win_icon)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=BIN_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BIN_NAME,
)

# ---------------------------------------------------------------------------
# macOS BUNDLE (.app)
# ---------------------------------------------------------------------------
if IS_MACOS:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon_path,
        bundle_identifier="com.autoapplynext.app",
        version="0.1.0",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "com.autoapplynext.app",
            "CFBundleVersion": "0.1.0",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": BIN_NAME,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSHumanReadableCopyright": "Copyright (c) AutoApply Next",
            # Network usage is for the engine talking to Seek / Anthropic.
            # Apple no longer requires NSAppTransportSecurity for outbound HTTPS,
            # but we leave a hook here for future per-domain rules.
        },
    )
