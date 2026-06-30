"""Build audience controls.

We ship the same codebase in two modes:
- tester: exposes diagnostic/manual screens and advanced controls.
- user: keeps the guided product surface for non-technical users.
"""

from __future__ import annotations

import os
import sys
from enum import Enum


class Audience(str, Enum):
    TESTER = "tester"
    USER = "user"


_ALIASES = {
    "test": Audience.TESTER,
    "tester": Audience.TESTER,
    "beta": Audience.TESTER,
    "internal": Audience.TESTER,
    "dev": Audience.TESTER,
    "user": Audience.USER,
    "consumer": Audience.USER,
    "public": Audience.USER,
    "production": Audience.USER,
    "prod": Audience.USER,
}


def current_audience() -> Audience:
    raw = (
        os.environ.get("AUTOAPPLY_BUILD_AUDIENCE")
        or os.environ.get("AUTOAPPLY_AUDIENCE")
        or ""
    ).strip().lower()
    if raw:
        return _ALIASES.get(raw, Audience.USER)

    # Source checkouts default to tester so local development keeps every
    # diagnostic screen. Frozen builds default to user unless the tester
    # launcher bakes in AUTOAPPLY_BUILD_AUDIENCE=tester.
    if getattr(sys, "frozen", False):
        return Audience.USER
    return Audience.TESTER


def is_tester_build() -> bool:
    return current_audience() is Audience.TESTER
