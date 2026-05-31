"""BatchScreen: live status monitor for an auto-apply run.

After the user clicked "Scrape and apply" on Queue, QueueScreen asks
MainWindow to swap to this screen. The worker's
`scrape_and_auto_apply` pipeline runs Phase 1 (scrape) then Phase 2
(apply each eligible queued job, score-desc, capped, throttled). Here
we surface live progress and the STOP button.

Per-job preview (cover letter + Q&A) is shown post-hoc as rows land;
the apply flow itself does NOT pause for human review, mirroring
job-finder's daemon (`vendor/job-finder/main.py`). The safety gate
(`SettingsStore.allow_real_submit`) is the only thing that distinguishes
real from dry-run; that choice + its confirmation live in Settings.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..engine.batch import BatchRunResult
from ..engine.results import ApplicationResult, ApplicationStatus
from ..engine.worker import EngineWorker
from ..safe_ui import safe_slot, show_error_dialog
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)


class BatchScreen(QWidget):
    """Status monitor for the scrape -> auto-apply pipeline.

    Trigger lives elsewhere (QueueScreen's "Scrape and apply"). This screen
    only renders progress + lets the user STOP."""

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
        # Maps url -> row index in the table so per-job updates land in
        # the right cell. New URLs append new rows.
        self._row_for_url: dict[str, int] = {}
        # Captured per-row results so the detail pane can show cover
        # letter / Q&A on click.
        self._results: list[ApplicationResult] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Batch (auto-apply)")
        title.setFont(_h1())
        layout.addWidget(title)

        layout.addWidget(self._build_status_bar())
        layout.addWidget(self._build_table_block(), stretch=1)
        layout.addWidget(self._build_tally())

        # Wire worker signals. Note: we no longer subscribe to
        # batch_prepare_*; the auto-apply flow does not run a prepare
        # phase. batch_apply_progress fires per job during the run.
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.batch_apply_progress.connect(self._on_run_progress)
        self._worker.batch_apply_finished.connect(self._on_run_finished)
        self._worker.scrape_finished.connect(self._on_scrape_finished)
        self._worker.log.connect(self._on_log)
        self._settings.allow_real_submit_changed.connect(self._refresh_mode_label)
        self._settings.match_threshold_changed.connect(self._refresh_threshold_label)

        self._refresh_threshold_label(self._settings.match_threshold)
        self._refresh_mode_label(self._settings.allow_real_submit)

    # ------------------------------------------------------------- widgets

    def _build_status_bar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("status-bar")
        wrap.setStyleSheet(
            "QFrame#status-bar { border: 1px solid #d1d5db; "
            "border-radius: 8px; padding: 8px; }"
        )
        h = QHBoxLayout(wrap)

        self._mode_label = QLabel()
        self._mode_label.setMinimumWidth(120)
        self._mode_label.setAlignment(Qt.AlignCenter)
        h.addWidget(self._mode_label)

        self._status_label = QLabel("Idle")
        self._status_label.setStyleSheet("color: #374151;")
        h.addWidget(self._status_label, stretch=1)

        self._threshold_label = QLabel()
        self._threshold_label.setStyleSheet("color: #6b7280;")
        h.addWidget(self._threshold_label)

        self._stop_btn = QPushButton("STOP batch")
        self._stop_btn.setStyleSheet(_stop_btn_css())
        self._stop_btn.setEnabled(False)
        self._stop_btn.setToolTip(
            "Halt the batch after the current job completes. The remaining "
            "jobs stay 'queued'; the next Scrape-and-apply picks them back up."
        )
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        h.addWidget(self._stop_btn)
        return wrap

    def _build_table_block(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)

        table_wrap = QWidget()
        tv = QVBoxLayout(table_wrap)
        tv.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["#", "Score", "Title", "Company", "Status"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 60)
        self._table.setColumnWidth(4, 130)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.currentCellChanged.connect(self._on_row_changed)
        tv.addWidget(self._table)
        splitter.addWidget(table_wrap)

        # Detail tabs: shown after a row completes (post-hoc review).
        detail_wrap = QWidget()
        dv = QVBoxLayout(detail_wrap)
        dv.setContentsMargins(0, 0, 0, 0)
        self._detail_header = QLabel(
            "Auto-apply runs without per-job preview; "
            "completed rows show their cover letter and Q&A here."
        )
        self._detail_header.setFont(_h2())
        self._detail_header.setWordWrap(True)
        dv.addWidget(self._detail_header)

        self._tabs = QTabWidget()
        self._cover_view = QPlainTextEdit()
        self._cover_view.setReadOnly(True)
        self._cover_view.setStyleSheet(_text_view_css())
        self._cover_view.setPlaceholderText("Cover letter (post-submit).")
        self._tabs.addTab(self._cover_view, "Cover letter")

        self._qa_view = QPlainTextEdit()
        self._qa_view.setReadOnly(True)
        self._qa_view.setStyleSheet(_text_view_css())
        self._qa_view.setPlaceholderText("Screening questions and answers.")
        self._tabs.addTab(self._qa_view, "Screening Q && A")

        self._error_view = QPlainTextEdit()
        self._error_view.setReadOnly(True)
        self._error_view.setStyleSheet(_text_view_css())
        self._error_view.setPlaceholderText("Error detail if any.")
        self._tabs.addTab(self._error_view, "Detail")

        dv.addWidget(self._tabs, stretch=1)
        splitter.addWidget(detail_wrap)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_tally(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("tally")
        wrap.setStyleSheet(
            "QFrame#tally { border: 1px solid #e5e7eb; "
            "border-radius: 8px; padding: 6px; background: #f9fafb; }"
        )
        h = QHBoxLayout(wrap)
        h.setContentsMargins(12, 6, 12, 6)
        self._tally_label = QLabel("No batch yet.")
        self._tally_label.setStyleSheet("color: #111827;")
        h.addWidget(self._tally_label)
        h.addStretch(1)
        self._run_progress = QProgressBar()
        self._run_progress.setRange(0, 1)
        self._run_progress.setValue(0)
        self._run_progress.setFixedWidth(220)
        self._run_progress.setVisible(False)
        h.addWidget(self._run_progress)
        return wrap

    # ------------------------------------------------------------- slots

    @Slot(int)
    def _refresh_threshold_label(self, value: int) -> None:
        self._threshold_label.setText(f"threshold >= {value}")

    @Slot(bool)
    def _refresh_mode_label(self, allowed: bool) -> None:
        if allowed:
            self._mode_label.setText("LIVE SUBMIT")
            self._mode_label.setStyleSheet(
                "padding: 4px 10px; background: #b91c1c; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )
        else:
            self._mode_label.setText("DRY-RUN")
            self._mode_label.setStyleSheet(
                "padding: 4px 10px; background: #15803d; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )

    @Slot(str)
    def _on_worker_state(self, state: str) -> None:
        running = state in ("running", "cancelling")
        self._stop_btn.setEnabled(running)
        self._run_progress.setVisible(running)
        if not running:
            self._status_label.setText("Idle")

    @Slot(str)
    @safe_slot
    def _on_log(self, line: str) -> None:
        # The worker emits its own scrape / auto-apply log lines via the
        # `log` signal. We surface the latest one as the status text so
        # the user always sees what stage we're in.
        # Filter to the worker's own milestone lines (those that start
        # with "Auto-apply" or "Scrap" or "STOP" or "[run").
        if any(line.startswith(p) for p in (
            "Auto-apply",
            "Scraping ",
            "Scraped ",
            "STOP ",
            "[run ",
            "[prepare ",
        )):
            self._status_label.setText(line)

    @Slot(object)
    @safe_slot
    def _on_scrape_finished(self, _result) -> None:
        # Auto-apply has its own batch_apply_progress events; we just
        # clear stale rows once scrape completes so the table reflects
        # the new run.
        self._table.setRowCount(0)
        self._row_for_url.clear()
        self._results.clear()

    @Slot()
    @safe_slot
    def _on_stop_clicked(self) -> None:
        self._worker.stop_batch()
        self._stop_btn.setEnabled(False)
        self._status_label.setText(
            self._status_label.text() + "  (STOP requested; finishing current job.)"
        )

    @Slot(int, int, object)
    @safe_slot
    def _on_run_progress(
        self, done: int, total: int, result: ApplicationResult
    ) -> None:
        self._run_progress.setRange(0, total)
        self._run_progress.setValue(done)
        # Append a new row if first sight; update existing row otherwise.
        url = result.job_url
        if url not in self._row_for_url:
            row_idx = self._table.rowCount()
            self._table.insertRow(row_idx)
            self._row_for_url[url] = row_idx
            self._results.append(result)
        else:
            row_idx = self._row_for_url[url]
            self._results[row_idx] = result
        self._render_row(row_idx, result)
        self._status_label.setText(self._running_status(done, total, result))

    def _running_status(
        self, done: int, total: int, result: ApplicationResult
    ) -> str:
        verb = {
            ApplicationStatus.SUBMITTED: "submitted",
            ApplicationStatus.SUBMITTED_UNCERTAIN: "submitted (uncertain)",
            ApplicationStatus.DRY_RUN_VERIFIED: "dry-run verified",
            ApplicationStatus.SKIPPED_LOW_SCORE: "skipped (low score)",
            ApplicationStatus.FAILED: "failed",
            ApplicationStatus.CANCELLED: "cancelled",
        }.get(result.status, result.status.value)
        return f"Auto-apply: {done}/{total} -- last: {verb}"

    @Slot(object)
    @safe_slot
    def _on_run_finished(self, tally: BatchRunResult) -> None:
        self._run_progress.setVisible(False)
        self._stop_btn.setEnabled(False)
        readout = (
            f"Batch {tally.stop_reason}. "
            f"submitted {tally.submitted}, "
            f"verified {tally.verified}, "
            f"uncertain {tally.submitted_uncertain} (verify on Seek), "
            f"failed {tally.failed}, "
            f"dry-run-verified {tally.dry_run_verified}, "
            f"skipped {tally.skipped_low_score}, "
            f"cancelled {tally.cancelled}."
        )
        if tally.fatal_reason:
            readout += f"\nFatal: {tally.fatal_reason}"
        self._tally_label.setText(readout)
        self._status_label.setText(f"Batch {tally.stop_reason}.")

    @Slot(str, str)
    @safe_slot
    def _on_worker_failed(self, op: str, msg: str) -> None:
        if op.startswith("batch_") or op == "scrape_and_auto_apply":
            show_error_dialog(self, f"Auto-apply error: {op}", msg)

    @Slot(int, int, int, int)
    @safe_slot
    def _on_row_changed(self, cur_row: int, _c: int, _pr: int, _pc: int) -> None:
        if cur_row < 0 or cur_row >= len(self._results):
            self._clear_detail()
            return
        result = self._results[cur_row]
        self._detail_header.setText(f"{result.job_url}")
        cover = result.cover_letter_text
        # Fall back to the sidecar file the adapter writes after tailor.
        if cover is None and result.cover_pdf is not None:
            for cand in [
                Path(str(result.cover_pdf) + ".txt"),
                self._engine_workdir / Path(str(result.cover_pdf) + ".txt"),
            ]:
                try:
                    if cand.exists():
                        cover = cand.read_text(encoding="utf-8")
                        break
                except Exception:
                    pass
        self._cover_view.setPlainText(
            cover or "(No cover letter for this row.)"
        )

        qa = result.screening_answers or []
        if qa:
            lines = [f"# {len(qa)} question(s) answered\n"]
            for q in qa:
                lines.append(
                    f"Q: {q.get('question', '').strip()}\n"
                    f"A: {q.get('answer', '').strip()}\n"
                    f"   source={q.get('source', '?')}\n"
                )
            self._qa_view.setPlainText("\n".join(lines))
        else:
            self._qa_view.setPlainText("(No screening Q & A recorded.)")

        if result.error_message:
            self._error_view.setPlainText(
                f"{result.exception_type or 'error'}: {result.error_message}"
            )
        else:
            self._error_view.setPlainText("(No error.)")

    # ------------------------------------------------------------- helpers

    def _clear_detail(self) -> None:
        self._detail_header.setText(
            "Select a completed row to see its cover letter and Q&A."
        )
        self._cover_view.clear()
        self._qa_view.clear()
        self._error_view.clear()

    def _render_row(self, row_idx: int, result: ApplicationResult) -> None:
        n = QTableWidgetItem(str(row_idx + 1))
        n.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row_idx, 0, n)

        score = result.score
        score_item = QTableWidgetItem(
            "" if score is None else f"{int(score):>3}"
        )
        score_item.setTextAlignment(Qt.AlignCenter)
        score_item.setBackground(_score_colour(score))
        self._table.setItem(row_idx, 1, score_item)

        # Title/company come from jobs.db via the result if the adapter
        # populated them; otherwise fall back to URL slug.
        title = self._title_for(result)
        company = self._company_for(result)
        title_item = QTableWidgetItem(title)
        title_item.setToolTip(result.job_url)
        self._table.setItem(row_idx, 2, title_item)
        self._table.setItem(row_idx, 3, QTableWidgetItem(company))

        status_text = result.status.value
        status_item = QTableWidgetItem(status_text)
        status_item.setForeground(_status_colour(result.status))
        font = status_item.font()
        font.setBold(True)
        status_item.setFont(font)
        self._table.setItem(row_idx, 4, status_item)

    def _title_for(self, result: ApplicationResult) -> str:
        # apply_to_job's result does not currently carry title/company.
        # Read them from jobs.db lazily.
        try:
            import sqlite3
            with sqlite3.connect(self._engine_workdir / "jobs.db") as conn:
                row = conn.execute(
                    "SELECT title FROM applications WHERE url = ?",
                    (result.job_url,),
                ).fetchone()
                if row and row[0]:
                    return row[0]
        except Exception:
            pass
        return result.job_url.rsplit("/", 1)[-1]

    def _company_for(self, result: ApplicationResult) -> str:
        try:
            import sqlite3
            with sqlite3.connect(self._engine_workdir / "jobs.db") as conn:
                row = conn.execute(
                    "SELECT company FROM applications WHERE url = ?",
                    (result.job_url,),
                ).fetchone()
                if row and row[0]:
                    return row[0]
        except Exception:
            pass
        return ""


# --------------------------------------------------------------------------- styles


def _score_colour(score: int | None) -> QColor:
    if score is None:
        return QColor("#f3f4f6")
    if score >= 70:
        return QColor("#bbf7d0")
    if score >= 30:
        return QColor("#fef3c7")
    return QColor("#fee2e2")


def _status_colour(status: ApplicationStatus) -> QColor:
    if status == ApplicationStatus.SUBMITTED:
        return QColor("#15803d")
    if status == ApplicationStatus.SUBMITTED_UNCERTAIN:
        return QColor("#c2410c")
    if status == ApplicationStatus.DRY_RUN_VERIFIED:
        return QColor("#1d4ed8")
    if status in (ApplicationStatus.SKIPPED_LOW_SCORE,):
        return QColor("#6b7280")
    if status == ApplicationStatus.CANCELLED:
        return QColor("#374151")
    return QColor("#b91c1c")


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


def _stop_btn_css() -> str:
    return (
        "QPushButton { background: #b91c1c; color: white; padding: 8px 16px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #fecaca; color: #fff; }"
    )


def _text_view_css() -> str:
    return (
        "QPlainTextEdit { background: #f8fafc; color: #111827; "
        "font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }"
    )
