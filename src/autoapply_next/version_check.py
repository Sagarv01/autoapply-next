"""In-app version floor (Phase F).

The proxy publishes a minimum client version via /api/config and rejects
below-floor clients with HTTP 426. This compares the running app's version to
that floor so the app can show an update-required screen before the user hits a
wall mid-run. Fails OPEN: an unknown or dev version (0.0.0 / unparseable) is never
forced to update, so a developer build is never locked out by a stale floor.
"""
from __future__ import annotations

from autoapply_next.config import download_url


def _parse(v) -> tuple[int, ...] | None:
    try:
        parts = [int(p) for p in str(v).strip().split(".")[:3]]
    except (ValueError, AttributeError):
        return None
    if not parts:
        return None
    return tuple(parts) + (0,) * (3 - len(parts))


def is_update_required(current: str, minimum: str) -> bool:
    """True only when `current` is a known, parseable version strictly below the
    known, parseable `minimum` floor. Anything unknown -> False (fail open)."""
    cur = _parse(current)
    mn = _parse(minimum)
    if cur is None or mn is None or cur == (0, 0, 0):
        return False
    return cur < mn
