"""QueueScreen: scraped jobs awaiting decision.

Reads jobs from the engine's `jobs.db`. Stub for Phase 2: just reads the
existing rows without re-scraping. Slice 4 wires a manual refresh that runs
the engine's scraper for one skill keyword and updates the table.

Selecting a row sets `selected_job_url` in the settings store; the Run screen
picks it up on next visit.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class QueueScreen(QWidget):
    def __init__(self, *, engine_workdir: Path):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._db_path = engine_workdir / "jobs.db"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Queue")
        title.setFont(_h1())
        layout.addWidget(title)

        row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh from DB")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        row.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #6b7280;")
        row.addWidget(self._count_label)
        layout.addLayout(row)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Score", "Title", "Company", "Status", "URL"]
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self._table, stretch=1)

        note = QLabel(
            "Click a row, then go to Run to dry-run that job. The scraper is "
            "wired in slice 4; for the skeleton, this table reads whatever is "
            "already in jobs.db."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(note)

        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        if not self._db_path.exists():
            self._table.setRowCount(0)
            self._count_label.setText(f"No jobs.db at {self._db_path}")
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT match_score, title, company, status, url "
                    "FROM applications ORDER BY timestamp DESC LIMIT 200"
                )
                rows = cur.fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("QueueScreen: SQLite read failed: %s", exc)
            self._table.setRowCount(0)
            self._count_label.setText(f"DB read failed: {exc}")
            return

        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            score = row["match_score"]
            self._table.setItem(
                i, 0, QTableWidgetItem("" if score is None else str(score))
            )
            self._table.setItem(i, 1, QTableWidgetItem(row["title"] or ""))
            self._table.setItem(i, 2, QTableWidgetItem(row["company"] or ""))
            self._table.setItem(i, 3, QTableWidgetItem(row["status"] or ""))
            self._table.setItem(i, 4, QTableWidgetItem(row["url"] or ""))
        self._count_label.setText(f"{len(rows)} jobs")


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
