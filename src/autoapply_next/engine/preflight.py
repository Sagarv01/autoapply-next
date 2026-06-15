"""TASKS 5.1/5.2: the hard preflight gate.

Eight coded checks that must all be green before the engine may start, each
deep-linking to the onboarding wizard step that fixes it. `run_preflight`
returns the red checks; `assert_ready` raises PreflightBlocked carrying all of
them. There is no bypass flag: this is the single gate and it cannot be skipped
(protected invariant). Local checks read the engine workdir's config/assets; the
live checks (entitlement, proxy reachability, kill switch, browser) are passed
in by the caller, which owns the proxy client and browser probe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class PreflightCode(str, Enum):
    MISSING_RESUME = "MISSING_RESUME"
    INCOMPLETE_ANSWER_BANK = "INCOMPLETE_ANSWER_BANK"
    NO_CRITERIA = "NO_CRITERIA"
    SEEK_SESSION_EXPIRED = "SEEK_SESSION_EXPIRED"
    ENTITLEMENT_INACTIVE = "ENTITLEMENT_INACTIVE"
    BROWSER_MISSING = "BROWSER_MISSING"
    PROXY_UNREACHABLE = "PROXY_UNREACHABLE"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"


# Deep-link: the onboarding wizard step (Phase 4) that fixes each failure.
# KILL_SWITCH_ACTIVE is not user-fixable, so it maps to None.
WIZARD_STEP: dict[PreflightCode, str | None] = {
    PreflightCode.MISSING_RESUME: "profile",
    PreflightCode.INCOMPLETE_ANSWER_BANK: "answers",
    PreflightCode.NO_CRITERIA: "criteria",
    PreflightCode.SEEK_SESSION_EXPIRED: "seek_connect",
    PreflightCode.ENTITLEMENT_INACTIVE: "account",
    PreflightCode.BROWSER_MISSING: "environment",
    PreflightCode.PROXY_UNREACHABLE: "environment",
    PreflightCode.KILL_SWITCH_ACTIVE: None,
}

# The screening answer bank the engine must have explicit answers for (blueprint
# 4.4); these are the config `candidate` keys the vendored seek_apply reads. They
# must be set by the user, not left to the engine's silent defaults.
REQUIRED_ANSWER_BANK_FIELDS = (
    "has_work_rights",
    "has_drivers_licence",
    "notice_period",
    "salary_target_aud",
    "years_experience",
    "willing_to_relocate",
)


@dataclass(frozen=True)
class PreflightError:
    code: PreflightCode
    message: str
    wizard_step: str | None
    detail: dict[str, Any] = field(default_factory=dict)


class PreflightBlocked(RuntimeError):
    """Raised when the engine is asked to start with a red preflight. Carries
    every red check so the UI can surface all of them with their deep-links."""

    def __init__(self, errors: list[PreflightError]):
        self.errors = errors
        super().__init__("Preflight blocked: " + ", ".join(e.code.value for e in errors))


# ──────────────────────────────────────────────── local (workdir) checks
def _load_config(workdir: Path) -> dict:
    p = Path(workdir) / "config.yaml"
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _resume_present(workdir: Path) -> bool:
    prof = Path(workdir) / "assets" / "profile.txt"
    try:
        return prof.exists() and prof.stat().st_size > 0
    except OSError:
        return False


def _missing_answer_bank(cfg: dict) -> list[str]:
    cand = cfg.get("candidate") if isinstance(cfg, dict) else None
    cand = cand if isinstance(cand, dict) else {}
    return [k for k in REQUIRED_ANSWER_BANK_FIELDS if cand.get(k) in (None, "")]


def _criteria_present(cfg: dict) -> bool:
    search = cfg.get("search") if isinstance(cfg, dict) else None
    search = search if isinstance(search, dict) else {}
    skills = search.get("skills")
    has_skills = bool(skills)
    has_location = bool(search.get("location"))
    return has_skills and has_location


def _seek_session_present(workdir: Path) -> bool:
    """A captured session: a non-trivial state.json or a populated chrome
    profile dir. Live expiry is probed at runtime (Phase 7.1); preflight only
    checks that a session was captured at all."""
    state = Path(workdir) / "sessions" / "seek" / "state.json"
    profile = Path(workdir) / "sessions" / "seek_chrome_profile"
    try:
        if state.is_file() and state.stat().st_size > 2:
            return True
        if profile.is_dir() and any(profile.iterdir()):
            return True
    except OSError:
        return False
    return False


def run_preflight(
    workdir,
    *,
    entitlement_active: bool,
    proxy_reachable: bool,
    submissions_enabled: bool,
    browser_available: bool,
) -> list[PreflightError]:
    """Evaluate all eight checks and return the red ones (empty == ready)."""
    workdir = Path(workdir)
    cfg = _load_config(workdir)
    errors: list[PreflightError] = []

    def add(code: PreflightCode, message: str, detail: dict | None = None) -> None:
        errors.append(PreflightError(code, message, WIZARD_STEP[code], detail or {}))

    if not _resume_present(workdir):
        add(PreflightCode.MISSING_RESUME,
            "No resume found. Add your resume so the bot can tailor applications.")

    missing = _missing_answer_bank(cfg)
    if missing:
        add(PreflightCode.INCOMPLETE_ANSWER_BANK,
            "Your screening answer bank is missing required answers: " + ", ".join(missing) + ".",
            {"missing": missing})

    if not _criteria_present(cfg):
        add(PreflightCode.NO_CRITERIA,
            "No job criteria set. Add titles/keywords and a location to search.")

    if not _seek_session_present(workdir):
        add(PreflightCode.SEEK_SESSION_EXPIRED,
            "Your Seek session is missing or expired. Sign in to Seek again.")

    if not entitlement_active:
        add(PreflightCode.ENTITLEMENT_INACTIVE,
            "Your subscription is inactive. Reactivate it to start applying.")

    if not browser_available:
        add(PreflightCode.BROWSER_MISSING,
            "The bundled browser is unavailable. Reinstall or repair the app.")

    if not proxy_reachable:
        add(PreflightCode.PROXY_UNREACHABLE,
            "Can't reach the AutoApply service. Check your internet connection.")

    if not submissions_enabled:
        add(PreflightCode.KILL_SWITCH_ACTIVE,
            "Applications are temporarily paused by AutoApply. Please try again later.")

    return errors


def assert_ready(
    workdir,
    *,
    entitlement_active: bool,
    proxy_reachable: bool,
    submissions_enabled: bool,
    browser_available: bool,
) -> None:
    """Raise PreflightBlocked if any check is red. This is the only engine-start
    gate and it has no bypass parameter (protected invariant)."""
    errors = run_preflight(
        workdir,
        entitlement_active=entitlement_active,
        proxy_reachable=proxy_reachable,
        submissions_enabled=submissions_enabled,
        browser_available=browser_available,
    )
    if errors:
        raise PreflightBlocked(errors)
