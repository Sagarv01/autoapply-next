"""The onboarding profile-facts model: the approved 19-question screening set.

These are stored in `config.yaml`'s `candidate` block, exactly where the frozen
engine (`vendor/job-finder/seek_apply.py`) reads them, so the bot answers Seek
screening questions from the user's own confirmed facts. This module is the
single schema + load/save/validation used by the onboarding wizard, the profile
screen, and the completion gate.

Field spec (drives both the Qt form and the required-field gate):
- 16 core facts (incl. the 2 conditional visa fields), all required except the
  visa fields, which are required only when the user is not a citizen/PR.
- 4 EEO facts, optional, defaulting to "prefer_not_to_say" (never block
  completion). Per the approved Phase B decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactField:
    key: str
    label: str
    kind: str  # 'bool' | 'text' | 'int' | 'choice'
    required: bool = True
    choices: tuple[str, ...] = ()
    default: Any = None
    # Required only when the named other field is FALSEY (e.g. visa fields are
    # required only when `is_citizen` is False).
    required_unless: str | None = None
    help_text: str = ""


CANDIDATE_FIELDS: tuple[FactField, ...] = (
    FactField("has_work_rights", "Do you have the right to work in Australia?", "bool"),
    FactField("is_citizen", "Are you an Australian citizen or permanent resident?", "bool"),
    FactField("visa_label", "If not a citizen/PR, what visa do you hold?", "text",
              required_unless="is_citizen", help_text="e.g. 485 Temporary Graduate Visa"),
    FactField("visa_expiry", "Visa expiry date", "text", required_unless="is_citizen"),
    FactField("needs_sponsorship", "Do you require visa sponsorship?", "bool"),
    FactField("has_security_clearance", "Do you hold a security clearance?", "bool"),
    FactField("years_experience", "Total years of relevant experience", "text",
              help_text="e.g. 5+"),
    FactField("salary_target_aud", "Salary expectation (AUD per year)", "int"),
    FactField("notice_period", "Notice period / availability to start", "text",
              help_text="e.g. Immediately available, 2 weeks"),
    FactField("willing_to_relocate", "Are you willing to relocate?", "bool"),
    FactField("location", "Which city are you based in?", "text"),
    FactField("has_drivers_licence", "Do you have a current driver's licence?", "bool"),
    FactField("has_own_car", "Do you have access to your own car?", "bool"),
    FactField("work_arrangement", "Preferred work arrangement", "choice",
              choices=("any", "remote", "hybrid", "onsite"), default="any"),
    FactField("highest_education", "Highest education", "text",
              help_text="e.g. Master of Information Technology"),
    FactField("employment_status", "Current employment status", "choice",
              choices=("between_jobs", "employed", "freelance")),
    # EEO / diversity — optional, default "prefer_not_to_say".
    FactField("gender", "Gender (optional)", "choice", required=False,
              choices=("prefer_not_to_say", "male", "female", "non_binary"),
              default="prefer_not_to_say"),
    FactField("identifies_aboriginal", "Aboriginal or Torres Strait Islander origin (optional)",
              "choice", required=False, choices=("prefer_not_to_say", "yes", "no"),
              default="prefer_not_to_say"),
    FactField("has_disability", "Disability or long-term health condition (optional)",
              "choice", required=False, choices=("prefer_not_to_say", "yes", "no"),
              default="prefer_not_to_say"),
    FactField("is_veteran", "Defence Force / veteran (optional)", "choice", required=False,
              choices=("prefer_not_to_say", "yes", "no"), default="prefer_not_to_say"),
)

_BY_KEY = {f.key: f for f in CANDIDATE_FIELDS}
_INT_KEYS = {f.key for f in CANDIDATE_FIELDS if f.kind == "int"}


def _config_path(workdir) -> Path:
    return Path(workdir) / "config.yaml"


def _read_config(workdir) -> dict:
    p = _config_path(workdir)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile_facts: failed to read config.yaml: %s", exc)
        return {}


def load_facts(workdir) -> dict[str, Any]:
    """Current fact values from config.yaml's candidate block. Missing fields are
    None (so the gate sees them as unanswered); EEO fields fall back to their
    default ('prefer_not_to_say')."""
    cand = _read_config(workdir).get("candidate")
    cand = cand if isinstance(cand, dict) else {}
    out: dict[str, Any] = {}
    for f in CANDIDATE_FIELDS:
        if f.key in cand and cand[f.key] not in (None, ""):
            out[f.key] = cand[f.key]
        elif f.default is not None:
            out[f.key] = f.default
        else:
            out[f.key] = None
    return out


def _is_required(f: FactField, values: dict) -> bool:
    if not f.required:
        return False
    if f.required_unless is not None and values.get(f.required_unless):
        return False
    return True


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def missing_required(values: dict[str, Any]) -> list[str]:
    """Keys of required fields the user has not answered (respecting the
    conditional visa rule). Empty list == ready."""
    return [f.key for f in CANDIDATE_FIELDS if _is_required(f, values) and _is_blank(values.get(f.key))]


def is_complete(values: dict[str, Any]) -> bool:
    return not missing_required(values)


def save_facts(workdir, values: dict[str, Any]) -> None:
    """Merge fact values into config.yaml's candidate block, preserving identity
    (name/email/phone) and all other config blocks. Coerces int fields."""
    cfg = _read_config(workdir)
    cand = cfg.get("candidate")
    if not isinstance(cand, dict):
        cand = {}
    for key, value in values.items():
        if key not in _BY_KEY:
            continue
        if key in _INT_KEYS and not _is_blank(value):
            try:
                value = int(str(value).replace(",", "").strip())
            except (TypeError, ValueError):
                pass
        cand[key] = value
    cfg["candidate"] = cand
    _config_path(workdir).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
