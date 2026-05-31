"""ResultsScreen: per-job outcome history with cover letter + screening preview.

Reads from `jobs.db` for the application history, from the cover-letter
`.txt` sidecar (written by the adapter after `tailorer.tailor` runs) for
the cover body, and from `errors/applications.jsonl` for the per-job
journals (questions answered, validation errors). Both the cover letter
text and the screening Q&A are surfaced verbatim so the user can review
what would go out under their name before flipping `ALLOW_REAL_SUBMIT`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..engine.persistence import CannotRequeueError, requeue_job
from ..safe_ui import confirm_dialog, safe_slot, show_error_dialog

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
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #6b7280;")
        row.addWidget(self._count_label)
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

        self._header_label = QLabel("Select a row to see details.")
        self._header_label.setFont(_h2())
        self._header_label.setWordWrap(True)
        v.addWidget(self._header_label)

        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("color: #6b7280;")
        self._meta_label.setWordWrap(True)
        v.addWidget(self._meta_label)

        # Row of contextual action buttons: enabled only when valid for
        # the selected row's status. Re-queue only for 'failed'; verify
        # on Seek only for 'submitted_uncertain'.
        action_row = QHBoxLayout()
        self._requeue_btn = QPushButton("Manually re-queue")
        self._requeue_btn.setEnabled(False)
        self._requeue_btn.setToolTip(
            "Move a 'failed' row back to 'queued' so it can be picked up "
            "by the next batch. Only enabled for 'failed' rows; "
            "'submitted_uncertain' must be verified on Seek by hand."
        )
        self._requeue_btn.clicked.connect(self._on_requeue_clicked)
        action_row.addWidget(self._requeue_btn)
        self._verify_btn = QPushButton("Verify on Seek")
        self._verify_btn.setEnabled(False)
        self._verify_btn.setToolTip(
            "Open Seek's Applied Jobs page in your browser to check by hand. "
            "Used for 'submitted_uncertain' rows: the engine clicked submit "
            "but could not confirm. Re-queueing is intentionally blocked."
        )
        self._verify_btn.clicked.connect(self._on_verify_clicked)
        action_row.addWidget(self._verify_btn)
        action_row.addStretch(1)
        v.addLayout(action_row)

        self._tabs = QTabWidget()

        # Tab 1: cover letter
        self._cover_view = QPlainTextEdit()
        self._cover_view.setReadOnly(True)
        self._cover_view.setStyleSheet(_text_view_css())
        self._cover_view.setPlaceholderText(
            "Cover letter text will appear here once a job with a tailored "
            "cover letter is selected. The text is read from <cover_pdf>.txt "
            "alongside the PDF the engine generated."
        )
        self._tabs.addTab(self._cover_view, "Cover letter")

        # Tab 2: screening Q&A
        self._qa_view = QPlainTextEdit()
        self._qa_view.setReadOnly(True)
        self._qa_view.setStyleSheet(_text_view_css())
        self._qa_view.setPlaceholderText(
            "Screening questions and answers from the engine's journal will "
            "appear here."
        )
        self._tabs.addTab(self._qa_view, "Screening Q && A")

        # Tab 3: raw journal + DB row
        self._raw_view = QPlainTextEdit()
        self._raw_view.setReadOnly(True)
        self._raw_view.setStyleSheet(_text_view_css())
        self._raw_view.setPlaceholderText(
            "Raw journal + DB fields. Useful for debugging a failure."
        )
        self._tabs.addTab(self._raw_view, "Raw")

        v.addWidget(self._tabs, stretch=1)
        return wrap

    # ---------------------------------------------------------- behaviour

    @Slot()
    def _refresh(self) -> None:
        self._rows: list[dict] = []
        if not self._db_path.exists():
            self._table.setRowCount(0)
            self._count_label.setText(f"No jobs.db at {self._db_path}")
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
            self._count_label.setText(f"DB read failed: {exc}")
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
        self._count_label.setText(f"{len(self._rows)} rows")

    @Slot(int, int, int, int)
    def _on_row_changed(self, cur_row: int, _c: int, _pr: int, _pc: int) -> None:
        if cur_row < 0 or cur_row >= len(self._rows):
            self._reset_detail()
            return
        row = self._rows[cur_row]
        self._header_label.setText(
            f"{row.get('title') or '(no title)'} at {row.get('company') or '(no company)'}"
        )
        self._meta_label.setText(
            f"Score: {row.get('match_score')} | Status: {row.get('status')} | "
            f"When: {row.get('timestamp')}\n{row.get('url')}"
        )
        status = (row.get("status") or "").lower()
        self._requeue_btn.setEnabled(status == "failed")
        self._verify_btn.setEnabled(status == "submitted_uncertain")

        # Cover letter from sidecar.
        cover_pdf = row.get("cover_letter_file")
        if cover_pdf:
            sidecar = Path(self._engine_workdir) / Path(cover_pdf + ".txt")
            # The engine writes cover paths relative to cwd; the sidecar is
            # cwd-relative too. Try both.
            candidates = [
                Path(cover_pdf + ".txt"),
                self._engine_workdir / Path(cover_pdf + ".txt"),
                self._engine_workdir / Path(cover_pdf).name.replace(".pdf", ".pdf.txt"),
            ]
            cover_text = None
            cover_path_used: Path | None = None
            for cand in candidates:
                try:
                    if cand.exists():
                        cover_text = cand.read_text(encoding="utf-8")
                        cover_path_used = cand
                        break
                except Exception as exc:
                    logger.warning("cover sidecar read failed: %s", exc)
            if cover_text is not None:
                self._cover_view.setPlainText(
                    f"# Source: {cover_path_used}\n# PDF: {cover_pdf}\n\n"
                    f"{cover_text}"
                )
            else:
                self._cover_view.setPlainText(
                    f"(No cover letter sidecar found.)\n"
                    f"Expected at: {cover_pdf}.txt\n"
                    "Older runs predate the sidecar; only the PDF was written. "
                    f"Open the PDF directly: {cover_pdf}"
                )
        else:
            self._cover_view.setPlainText(
                "(No cover letter recorded for this row.)"
            )

        # Journal-driven Q&A.
        journal = self._find_journal(row.get("url"))
        if journal is None:
            self._qa_view.setPlainText("(No journal entry found for this URL.)")
        else:
            qs = journal.get("questions_answered") or []
            if not qs:
                self._qa_view.setPlainText(
                    "(No screening questions recorded for this run.)"
                )
            else:
                lines = [
                    f"# {len(qs)} question(s) answered\n"
                    "# source: hard-rule | 485-alias | claude | fallback\n"
                ]
                for q in qs:
                    lines.append(
                        f"Q: {q.get('question', '').strip()}\n"
                        f"A: {q.get('answer', '').strip()}\n"
                        f"   source={q.get('source', '?')}\n"
                    )
                    opts = q.get("options")
                    if opts:
                        lines.append(
                            "   options:\n" +
                            "\n".join(f"   - {o}" for o in opts) +
                            "\n"
                        )
                self._qa_view.setPlainText("\n".join(lines))

        # Raw view: row + journal.
        raw = {
            "db_row": row,
            "journal": journal,
        }
        self._raw_view.setPlainText(json.dumps(raw, indent=2, default=str))

    def _reset_detail(self) -> None:
        self._header_label.setText("Select a row to see details.")
        self._meta_label.setText("")
        self._cover_view.clear()
        self._qa_view.clear()
        self._raw_view.clear()
        self._requeue_btn.setEnabled(False)
        self._verify_btn.setEnabled(False)

    def _current_row(self) -> dict | None:
        idx = self._table.currentRow()
        if idx < 0 or idx >= len(self._rows):
            return None
        return self._rows[idx]

    @Slot()
    @safe_slot
    def _on_requeue_clicked(self) -> None:
        row = self._current_row()
        if row is None:
            return
        url = row.get("url") or ""
        confirmed = confirm_dialog(
            self,
            "Re-queue this failed job?",
            "This moves the row from 'failed' back to 'queued' so the next "
            f"batch can try again. Nothing was filed last time.\n\n{url}",
        )
        if not confirmed:
            return
        try:
            requeue_job(engine_workdir=self._engine_workdir, url=url)
        except CannotRequeueError as exc:
            show_error_dialog(self, "Cannot re-queue", str(exc))
            return
        self._refresh()

    @Slot()
    @safe_slot
    def _on_verify_clicked(self) -> None:
        # Open Seek's Applied Jobs page in the user's normal browser so
        # they can verify by eye. Deliberately not the job listing URL
        # (the listing won't show "applied" status), and deliberately
        # not the engine's automated browser (the verifier already tried).
        webbrowser.open("https://au.seek.com/my-activity/applied-jobs")

    def _find_journal(self, url: str | None) -> dict | None:
        if url is None or not self._journal_path.exists():
            return None
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
    f.setPointSize(14)
    f.setBold(True)
    return f


def _text_view_css() -> str:
    return (
        "QPlainTextEdit { background: #f8fafc; color: #111827; "
        "font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }"
    )
