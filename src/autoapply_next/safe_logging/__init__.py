"""Safe logging utilities for AutoApply Next.

Provides :class:`PIIScrubFilter` and :func:`install_global_scrubbing` so the
rest of the app can route logs through stdlib ``logging`` without worrying
about PII or secrets leaking to the GUI log tail, ``bot.log``, or future
crash reports.
"""

from __future__ import annotations

from .scrubber import PIIScrubFilter, install_global_scrubbing

__all__ = ["PIIScrubFilter", "install_global_scrubbing"]
