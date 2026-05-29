"""Platform-specific helpers for AutoApply Next.

This subpackage isolates the bits that need to know what OS we are on:
canonical user-scoped path resolution and Chrome executable discovery.
Keeping this in one place means the rest of the app never has to branch
on ``sys.platform``.
"""

from __future__ import annotations

from .paths import (
    app_cache_dir,
    app_data_dir,
    app_log_dir,
    engine_workdir,
    locate_chrome,
)

__all__ = [
    "app_cache_dir",
    "app_data_dir",
    "app_log_dir",
    "engine_workdir",
    "locate_chrome",
]
