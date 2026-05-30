"""QueueScreen: scrape, score, and pick jobs to run.

User flow:
1. Type a search keyword.
2. Click Refresh. Worker scrapes Seek for that keyword, scores each new
   listing, persists rows into `applications` with status="queued".
3. Table refreshes from `jobs.db` showing all jobs, sorted by score desc.
4. Click "Run dry-run" on a row to send the URL to the Run screen.
5. Optionally toggle "Hide below threshold" to filter weak matches.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..engine.scraping import ScrapeResult
from ..engine.worker import EngineWorker
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)


class QueueScreen(QWidget):
    run_requested = Signal(str)
    """Emitted with a Seek URL when the user picks a row to run. MainWindow
    catches this and switches to the Run screen with the URL preloaded."""

    def __init__(
        self,
        *,
        engine_workdir: Path,
        worker: EngineWorker,
        settings: SettingsStore,
    ):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._worker = worker
        self._settings = settings
        self._db_path = engine_workdir / "jobs.db"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Queue")
        title.setFont(_h1())
        layout.addWidget(title)

        layout.addWidget(self._build_scrape_row())
        layout.addWidget(self._build_filter_row())
        layout.addWidget(self._build_table(), stretch=1)
        layout.addWidget(self._build_status_row())

        # Worker signals
        self._worker.log.connect(self._on_log)
        self._worker.scrape_finished.connect(self._on_scrape_finished)
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.failed.connect(self._on_worker_failed)

        self._settings.match_threshold_changed.connect(
            lambda _v: self._refresh_table()
        )
        self._refresh_table()

    # ---------------------------------------------------------- widgets

    def _build_scrape_row(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("scrape-row")
        wrap.setStyleSheet(
            "QFrame#scrape-row { border: 1px solid #d1d5db; "
            "border-radius: 8px; padding: 8px; }"
        )
        h = QHBoxLayout(wrap)
        self._keyword_input = QLineEdit()
        self._keyword_input.setPlaceholderText(
            "Search keyword (e.g. 'devops engineer')"
        )
        last_kw = self._settings.last_scrape_keyword
        if last_kw:
            self._keyword_input.setText(last_kw)
        self._keyword_input.returnPressed.connect(self._on_refresh_clicked)
        h.addWidget(self._keyword_input, stretch=1)

        self._refresh_btn = QPushButton("Scrape and score")
        self._refresh_btn.setMinimumWidth(180)
        self._refresh_btn.setStyleSheet(_primary_btn())
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        h.addWidget(self._refresh_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._worker.cancel)
        h.addWidget(self._cancel_btn)
        return wrap

    def _build_filter_row(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)

        self._only_queued = QCheckBox("Only queued (not yet applied)")
        self._only_queued.setChecked(True)
        self._only_queued.toggled.connect(self._refresh_table)
        h.addWidget(self._only_queued)

        self._only_above_threshold = QCheckBox(
            f"Only score >= threshold ({self._settings.match_threshold})"
        )
        self._only_above_threshold.setChecked(False)
        self._only_above_threshold.toggled.connect(self._refresh_table)
        self._settings.match_threshold_changed.connect(
            lambda v: self._only_above_threshold.setText(
                f"Only score >= threshold ({v})"
            )
        )
        h.addWidget(self._only_above_threshold)

        h.addStretch(1)

        self._reload_btn = QPushButton("Reload table")
        self._reload_btn.clicked.connect(self._refresh_table)
        h.addWidget(self._reload_btn)
        return wrap

    def _build_table(self) -> QWidget:
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Score", "Title", "Company", "Status", "URL", ""]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setColumnWidth(0, 64)
        self._table.setColumnWidth(3, 80)
        self._table.setColumnWidth(5, 110)
        return self._table

    def _build_status_row(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(False)
        h.addWidget(self._progress_bar, stretch=1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #6b7280;")
        h.addWidget(self._count_label)
        return wrap

    # ---------------------------------------------------------- behaviour

    @Slot()
    def _on_refresh_clicked(self) -> None:
        kw = self._keyword_input.text().strip()
        if not kw:
            self._count_label.setText("Type a search keyword first.")
            return
        self._settings.last_scrape_keyword = kw
        self._count_label.setText(f"Scraping Seek for '{kw}'...")
        self._worker.scrape_and_score(kw)

    @Slot(str)
    def _on_log(self, line: str) -> None:
        # Treat the log signal as a status tick from the worker.
        self._count_label.setText(line)

    @Slot(object)
    def _on_scrape_finished(self, result: ScrapeResult) -> None:
        if result.errors:
            self._count_label.setText(
                f"Scraped {result.total_scraped}, {len(result.scored)} scored, "
                f"{len(result.errors)} errors (see log)"
            )
            for err in result.errors:
                logger.warning("scrape error: %s", err)
        else:
            self._count_label.setText(
                f"Scraped {result.total_scraped}, {len(result.scored)} scored. "
                "Click 'Run dry-run' to start."
            )
        self._refresh_table()

    @Slot(str)
    def _on_worker_state(self, state: str) -> None:
        running = state in ("running", "cancelling")
        self._refresh_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        self._progress_bar.setVisible(running)
        self._keyword_input.setEnabled(not running)

    @Slot(str, str)
    def _on_worker_failed(self, op: str, msg: str) -> None:
        if op == "scrape":
            self._count_label.setText(f"Scrape failed: {msg}")

    @Slot()
    def _refresh_table(self) -> None:
        rows = self._read_db()
        threshold = self._settings.match_threshold
        if self._only_queued.isChecked():
            rows = [r for r in rows if (r["status"] or "") == "queued"]
        if self._only_above_threshold.isChecked():
            rows = [
                r
                for r in rows
                if (r["match_score"] or 0) >= threshold
            ]
        rows.sort(key=lambda r: -(r["match_score"] or 0))

        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            score = row["match_score"]
            score_item = QTableWidgetItem(
                "" if score is None else f"{int(score):>3}"
            )
            score_item.setTextAlignment(Qt.AlignCenter)
            if score is not None:
                score_item.setBackground(_score_colour(int(score), threshold))
            self._table.setItem(i, 0, score_item)
            self._table.setItem(i, 1, QTableWidgetItem(row["title"] or ""))
            self._table.setItem(i, 2, QTableWidgetItem(row["company"] or ""))
            status_item = QTableWidgetItem(row["status"] or "")
            self._table.setItem(i, 3, status_item)
            url_item = QTableWidgetItem(row["url"] or "")
            url_item.setToolTip(row["url"] or "")
            self._table.setItem(i, 4, url_item)

            run_btn = QPushButton("Run dry-run")
            url = row["url"]
            run_btn.clicked.connect(lambda _=False, u=url: self.run_requested.emit(u))
            self._table.setCellWidget(i, 5, run_btn)

        if not rows:
            self._count_label.setText(
                "No jobs match the current filters. "
                "Scrape a keyword or untick 'Only queued'."
            )

    def _read_db(self) -> list[dict]:
        if not self._db_path.exists():
            return []
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT match_score, title, company, status, url, timestamp "
                    "FROM applications ORDER BY timestamp DESC LIMIT 500"
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            logger.warning("QueueScreen DB read failed: %s", exc)
            return []


def _score_colour(score: int, threshold: int) -> QColor:
    if score >= max(threshold, 70):
        return QColor("#bbf7d0")  # green
    if score >= threshold:
        return QColor("#fef3c7")  # amber
    return QColor("#fee2e2")  # red


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 8px 16px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #93c5fd; }"
    )
