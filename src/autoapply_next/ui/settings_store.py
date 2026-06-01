"""Persistent UI settings, JSON-backed.

Used by SettingsScreen and by MainWindow + RunScreen. Not used by the engine
adapter directly (the adapter takes its config via kwargs).

Tracked keys:
- allow_real_submit: bool. The gate flag. Default False.
- match_threshold: int (0 to 100). Default 20.
- daily_cap: int. Default 30.
- operating_hours_start / end: str ("HH:MM"). Default "07:00" / "23:00".
- selected_job_url: str | None. Whatever the Queue screen most recently picked.

Persistence: a single JSON file at the path passed to __init__. Loaded on
init, saved on every setter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, object] = {
    "allow_real_submit": False,
    # Lowered from 50 to 10 for the batch-apply flow: when the user is
    # working through the whole queue with review-then-run, weak matches
    # are easy to deselect at the approve step, but jobs missing from the
    # prepare list cannot be considered at all. 10 is permissive enough
    # that almost every scored job appears for review; the user can still
    # tune in Settings. Existing on-disk settings files keep their value.
    "match_threshold": 10,
    "daily_cap": 30,
    "operating_hours_start": "07:00",
    "operating_hours_end": "23:00",
    "selected_job_url": None,
    "last_scrape_keyword": "",
    # Seconds between live submissions in a batch. Throttled against Seek's
    # anti-bot. The engine daemon historically waited 30 to 90s; for the
    # interactive batch we default lower because the user is watching.
    "batch_throttle_seconds": 20,
    # When True (default, recommended), live submissions wait 60-120s
    # between each other, mirroring job-finder's APPLY_GAP_MIN/MAX. When
    # False, applies run back-to-back with no pause. This is a deliberate
    # opt-out: skipping the throttle increases the chance Seek's anti-bot
    # heuristics flag the account. Surface as a checkbox in Settings.
    "pace_between_applies": True,
}


class SettingsStore(QObject):
    allow_real_submit_changed = Signal(bool)
    match_threshold_changed = Signal(int)
    daily_cap_changed = Signal(int)
    selected_job_url_changed = Signal(str)
    pace_between_applies_changed = Signal(bool)

    def __init__(self, path: Path):
        super().__init__()
        self._path = Path(path)
        self._data: dict[str, object] = dict(DEFAULTS)
        self._load()

    # --------------------------------------------------------------- props

    @property
    def allow_real_submit(self) -> bool:
        return bool(self._data["allow_real_submit"])

    @allow_real_submit.setter
    def allow_real_submit(self, value: bool) -> None:
        value = bool(value)
        if value == self.allow_real_submit:
            return
        self._data["allow_real_submit"] = value
        self._save()
        self.allow_real_submit_changed.emit(value)
        logger.warning(
            "SettingsStore: allow_real_submit set to %s",
            value,
        )

    @property
    def match_threshold(self) -> int:
        return int(self._data["match_threshold"])

    @match_threshold.setter
    def match_threshold(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value == self.match_threshold:
            return
        self._data["match_threshold"] = value
        self._save()
        self.match_threshold_changed.emit(value)

    @property
    def daily_cap(self) -> int:
        return int(self._data["daily_cap"])

    @daily_cap.setter
    def daily_cap(self, value: int) -> None:
        value = max(0, int(value))
        if value == self.daily_cap:
            return
        self._data["daily_cap"] = value
        self._save()
        self.daily_cap_changed.emit(value)

    @property
    def selected_job_url(self) -> str | None:
        v = self._data["selected_job_url"]
        return v if isinstance(v, str) and v else None

    @selected_job_url.setter
    def selected_job_url(self, value: str | None) -> None:
        if value == self.selected_job_url:
            return
        self._data["selected_job_url"] = value
        self._save()
        if value:
            self.selected_job_url_changed.emit(value)

    @property
    def last_scrape_keyword(self) -> str:
        v = self._data.get("last_scrape_keyword", "")
        return v if isinstance(v, str) else ""

    @last_scrape_keyword.setter
    def last_scrape_keyword(self, value: str) -> None:
        value = str(value)
        if value == self.last_scrape_keyword:
            return
        self._data["last_scrape_keyword"] = value
        self._save()

    @property
    def batch_throttle_seconds(self) -> int:
        v = self._data.get("batch_throttle_seconds", 20)
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 20

    @batch_throttle_seconds.setter
    def batch_throttle_seconds(self, value: int) -> None:
        value = max(0, int(value))
        if value == self.batch_throttle_seconds:
            return
        self._data["batch_throttle_seconds"] = value
        self._save()

    @property
    def pace_between_applies(self) -> bool:
        """If True (default), the worker enforces a 60-120s random gap
        between live applies (job-finder APPLY_GAP_MIN/MAX parity). If
        False the worker passes throttle 0 to run_batch and applies
        run back-to-back. Off is a deliberate opt-out: skipping the
        pace makes Seek's anti-bot heuristics more likely to flag the
        account."""
        return bool(self._data.get("pace_between_applies", True))

    @pace_between_applies.setter
    def pace_between_applies(self, value: bool) -> None:
        value = bool(value)
        if value == self.pace_between_applies:
            return
        self._data["pace_between_applies"] = value
        self._save()
        self.pace_between_applies_changed.emit(value)
        logger.info(
            "SettingsStore: pace_between_applies set to %s%s",
            value,
            "" if value else " (no throttle between applies)",
        )

    # --------------------------------------------------------------- io

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                loaded = json.load(f)
            for k, v in loaded.items():
                if k in self._data:
                    self._data[k] = v
        except Exception as exc:
            logger.warning(
                "SettingsStore: failed to load %s (%s); using defaults",
                self._path,
                exc,
            )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning(
                "SettingsStore: failed to save %s (%s)", self._path, exc
            )
