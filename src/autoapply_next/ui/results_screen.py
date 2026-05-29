"""ResultsScreen: per-job outcome history with cover letter + screening preview.

Reads from `jobs.db` for the application history, and from
`errors/applications.jsonl` for per-job journals (questions answered, source,
errors). Clicking a row shows the cover letter PDF path and the screening
answers verbatim so the user can review what would go out under their name.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class ResultsScreen(QWidget):
    def __init__(self, *, engine_workdir: Path):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._db_path = engine_workdir / "jobs.db"
        self._journal_path = engine_workdir / "errors" / "applications.jsonl"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Results")
        title.setFont(_h1())
        layout.addWidget(title)

        row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        row.addStretch(1)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_table())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, stretch=1)

        self._refresh()

    # ---------------------------------------------------------- widgets

    def _build_table(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["When", "Title @ Company", "Score", "Status"]
        )
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.currentCellChanged.connect(self._on_row_changed)
        v.addWidget(self._table)
        return wrap

    def _build_detail(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Selected application")
        header.setFont(_h2())
        v.addWidget(header)

        self._detail_view = QPlainTextEdit()
        self._detail_view.setReadOnly(True)
        self._detail_view.setStyleSheet(
            "QPlainTextEdit { background: #f9fafb; color: #111827; "
            "font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }"
        )
        self._detail_view.setPlaceholderText("Select a row to see details.")
        v.addWidget(self._detail_view, stretch=1)
        return wrap

    # ---------------------------------------------------------- behaviour

    @Slot()
    def _refresh(self) -> None:
        self._rows: list[dict] = []
        if not self._db_path.exists():
            self._table.setRowCount(0)
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT timestamp, title, company, match_score, status, "
                    "url, resume_file, cover_letter_file, notes "
                    "FROM applications ORDER BY timestamp DESC LIMIT 200"
                )
                self._rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            logger.warning("ResultsScreen: SQLite read failed: %s", exc)
            self._table.setRowCount(0)
            return

        self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            self._table.setItem(i, 0, QTableWidgetItem(str(row.get("timestamp") or "")))
            self._table.setItem(
                i,
                1,
                QTableWidgetItem(
                    f"{row.get('title') or ''} @ {row.get('company') or ''}"
                ),
            )
            self._table.setItem(
                i, 2, QTableWidgetItem(str(row.get("match_score") or ""))
            )
            self._table.setItem(i, 3, QTableWidgetItem(str(row.get("status") or "")))

    @Slot(int, int, int, int)
    def _on_row_changed(self, cur_row: int, _c: int, _pr: int, _pc: int) -> None:
        if cur_row < 0 or cur_row >= len(self._rows):
            self._detail_view.clear()
            return
        row = self._rows[cur_row]
        journal = self._find_journal(row.get("url"))
        lines = [
            f"URL:         {row.get('url')}",
            f"Title:       {row.get('title')}",
            f"Company:     {row.get('company')}",
            f"Status:      {row.get('status')}",
            f"Score:       {row.get('match_score')}",
            f"Resume PDF:  {row.get('resume_file')}",
            f"Cover PDF:   {row.get('cover_letter_file')}",
            f"Notes:       {row.get('notes')}",
            "",
        ]
        if journal is None:
            lines.append("(no journal entry found for this URL)")
        else:
            qs = journal.get("questions_answered") or []
            lines.append(f"Questions answered: {len(qs)}")
            for q in qs:
                lines.append(
                    f"  Q: {q.get('question')!r}"
                )
                lines.append(
                    f"     -> {q.get('answer')!r}  [{q.get('source')}]"
                )
            errs = journal.get("validation_errors_seen") or []
            if errs:
                lines.append("")
                lines.append(f"Validation errors: {len(errs)}")
                for e in errs:
                    lines.append(f"  {e.get('question')!r}: {e.get('error')!r}")
            final_error = journal.get("final_error")
            if final_error:
                lines.append("")
                lines.append(f"Final error: {final_error}")
        self._detail_view.setPlainText("\n".join(lines))

    def _find_journal(self, url: str | None) -> dict | None:
        if url is None or not self._journal_path.exists():
            return None
        # Read backwards for the most recent entry with this URL.
        try:
            with open(self._journal_path) as f:
                lines = f.readlines()
        except Exception as exc:
            logger.warning("ResultsScreen: journal read failed: %s", exc)
            return None
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("url") == url:
                return entry
        return None


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _h2() -> QFont:
    f = QFont()
    f.setPointSize(13)
    f.setBold(True)
    return f
