"""BatchScreen: review-then-run batch apply.

Three sections, top to bottom:

  1. Prepare bar: threshold display, "Prepare batch" button, cancel,
     progress label.
  2. Approve table: one row per prepared job, with a checkbox, score,
     title @ company, status pill, and a "View cover letter / Q&A"
     expander into a side detail pane.
  3. Run bar: "Select all / Deselect all", "Submit N selected", STOP
     (visible during run), tally readout (submitted / verified / failed /
     skipped / cancelled), stop-reason on completion.

Default is DRY_RUN. The Run bar's primary button reads "Submit N (dry-run)"
or "Submit N (LIVE)" depending on the SettingsStore gate. The pre-run
confirmation dialog spells out the exact count and mode.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
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

from ..engine.batch import BatchPreparedJob, BatchRunResult
from ..engine.results import ApplicationResult, ApplicationStatus
from ..engine.worker import EngineWorker
from ..safe_ui import confirm_dialog, safe_slot, show_error_dialog
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)


PREPARE_MAX_JOBS = 30


class BatchScreen(QWidget):
    def __init__(
        self, *, engine_workdir: Path, worker: EngineWorker, settings: SettingsStore
    ):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._worker = worker
        self._settings = settings
        self._prepared: list[BatchPreparedJob] = []
        self._row_checkboxes: list[QCheckBox] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Batch apply")
        title.setFont(_h1())
        layout.addWidget(title)

        layout.addWidget(self._build_prepare_bar())
        layout.addWidget(self._build_table_block(), stretch=1)
        layout.addWidget(self._build_run_bar())
        layout.addWidget(self._build_tally())

        # Wire worker signals (we only handle batch + state).
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.batch_prepare_progress.connect(self._on_prepare_progress)
        self._worker.batch_prepare_finished.connect(self._on_prepare_finished)
        self._worker.batch_apply_progress.connect(self._on_run_progress)
        self._worker.batch_apply_finished.connect(self._on_run_finished)
        self._settings.allow_real_submit_changed.connect(self._refresh_submit_label)
        self._settings.match_threshold_changed.connect(self._refresh_threshold_label)

        self._refresh_threshold_label(self._settings.match_threshold)
        self._refresh_submit_label(self._settings.allow_real_submit)

    # ----------------------------------------------------------- widgets

    def _build_prepare_bar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("prepare-bar")
        wrap.setStyleSheet(
            "QFrame#prepare-bar { border: 1px solid #d1d5db; "
            "border-radius: 8px; padding: 8px; }"
        )
        h = QHBoxLayout(wrap)

        self._threshold_label = QLabel()
        self._threshold_label.setStyleSheet("color: #374151;")
        h.addWidget(self._threshold_label)

        h.addStretch(1)

        self._prepare_btn = QPushButton("Prepare batch")
        self._prepare_btn.setStyleSheet(_primary_btn())
        self._prepare_btn.setToolTip(
            "For every queued job at or above the threshold, dry-run "
            "the engine and capture the cover letter + screening answers. "
            "Nothing is submitted yet."
        )
        self._prepare_btn.clicked.connect(self._on_prepare_clicked)
        h.addWidget(self._prepare_btn)

        self._prepare_cancel_btn = QPushButton("Cancel prepare")
        self._prepare_cancel_btn.setEnabled(False)
        self._prepare_cancel_btn.clicked.connect(self._worker.cancel)
        h.addWidget(self._prepare_cancel_btn)

        self._prepare_progress = QProgressBar()
        self._prepare_progress.setRange(0, 0)
        self._prepare_progress.setVisible(False)
        self._prepare_progress.setFixedWidth(180)
        h.addWidget(self._prepare_progress)
        return wrap

    def _build_table_block(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)

        # Left: table.
        table_wrap = QWidget()
        tv = QVBoxLayout(table_wrap)
        tv.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["✓", "Score", "Title", "Company", "Status", "URL"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        self._table.setColumnWidth(0, 32)
        self._table.setColumnWidth(1, 60)
        self._table.setColumnWidth(4, 110)
        self._table.setColumnWidth(5, 60)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.currentCellChanged.connect(self._on_row_changed)
        tv.addWidget(self._table)
        splitter.addWidget(table_wrap)

        # Right: detail tabs (cover letter + Q&A + raw).
        detail_wrap = QWidget()
        dv = QVBoxLayout(detail_wrap)
        dv.setContentsMargins(0, 0, 0, 0)
        self._detail_header = QLabel("Select a row to preview.")
        self._detail_header.setFont(_h2())
        self._detail_header.setWordWrap(True)
        dv.addWidget(self._detail_header)

        self._tabs = QTabWidget()
        self._cover_view = QPlainTextEdit()
        self._cover_view.setReadOnly(True)
        self._cover_view.setStyleSheet(_text_view_css())
        self._cover_view.setPlaceholderText("Cover letter preview.")
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

    def _build_run_bar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("run-bar")
        wrap.setStyleSheet(
            "QFrame#run-bar { border: 1px solid #d1d5db; "
            "border-radius: 8px; padding: 8px; }"
        )
        h = QHBoxLayout(wrap)

        self._select_all_btn = QPushButton("Select all ready")
        self._select_all_btn.clicked.connect(self._on_select_all_clicked)
        self._select_all_btn.setEnabled(False)
        h.addWidget(self._select_all_btn)

        self._deselect_all_btn = QPushButton("Deselect all")
        self._deselect_all_btn.clicked.connect(self._on_deselect_all_clicked)
        self._deselect_all_btn.setEnabled(False)
        h.addWidget(self._deselect_all_btn)

        h.addStretch(1)

        self._selected_label = QLabel("0 selected")
        self._selected_label.setStyleSheet("color: #6b7280;")
        h.addWidget(self._selected_label)

        self._submit_btn = QPushButton("Submit 0 (dry-run)")
        self._submit_btn.setMinimumWidth(200)
        self._submit_btn.setStyleSheet(_primary_btn())
        self._submit_btn.setEnabled(False)
        self._submit_btn.clicked.connect(self._on_submit_clicked)
        h.addWidget(self._submit_btn)

        self._stop_btn = QPushButton("STOP batch")
        self._stop_btn.setStyleSheet(
            "QPushButton { background: #b91c1c; color: white; padding: 8px 16px; "
            "border-radius: 6px; font-weight: bold; }"
            "QPushButton:disabled { background: #fecaca; color: #fff; }"
        )
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        h.addWidget(self._stop_btn)
        return wrap

    def _build_tally(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("tally")
        wrap.setStyleSheet(
            "QFrame#tally { border: 1px solid #e5e7eb; "
            "border-radius: 8px; padding: 6px; background: #f9fafb; }"
        )
        h = QHBoxLayout(wrap)
        h.setContentsMargins(12, 6, 12, 6)
        self._tally_label = QLabel("No batch run yet.")
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

    # ----------------------------------------------------------- handlers

    @Slot(int)
    def _refresh_threshold_label(self, value: int) -> None:
        self._threshold_label.setText(
            f"Prepare jobs with score >= {value} (change in Settings, max "
            f"{PREPARE_MAX_JOBS} per prepare). Default is 10."
        )

    @Slot(bool)
    def _refresh_submit_label(self, allowed: bool) -> None:
        self._update_submit_button()
        # Re-style if running -- the badge separately reflects the gate; we
        # only colour the button.

    def _update_submit_button(self) -> None:
        count = self._selected_count()
        live = self._settings.allow_real_submit
        text = (
            f"Submit {count} (LIVE)" if live else f"Submit {count} (dry-run)"
        )
        self._submit_btn.setText(text)
        if live:
            self._submit_btn.setStyleSheet(
                "QPushButton { background: #b91c1c; color: white; padding: 8px 16px; "
                "border-radius: 6px; font-weight: bold; }"
                "QPushButton:disabled { background: #fecaca; }"
            )
        else:
            self._submit_btn.setStyleSheet(_primary_btn())
        self._submit_btn.setEnabled(count > 0 and self._worker_idle())
        self._selected_label.setText(f"{count} selected")

    def _selected_count(self) -> int:
        return sum(1 for cb in self._row_checkboxes if cb.isChecked())

    def _approved_urls(self) -> list[str]:
        urls = []
        for cb, row in zip(self._row_checkboxes, self._prepared):
            if cb.isChecked() and row.ready:
                urls.append(row.url)
        return urls

    def _worker_idle(self) -> bool:
        # state_changed handler tracks this; recompute defensively via the
        # button's own state (Prepare button disabled = worker busy).
        return self._prepare_btn.isEnabled()

    @Slot()
    @safe_slot
    def _on_prepare_clicked(self) -> None:
        self._prepared = []
        self._row_checkboxes = []
        self._table.setRowCount(0)
        self._tally_label.setText(
            f"Preparing jobs with score >= {self._settings.match_threshold}..."
        )
        self._worker.prepare_batch(
            self._settings.match_threshold, max_jobs=PREPARE_MAX_JOBS
        )

    @Slot(int, int, object)
    @safe_slot
    def _on_prepare_progress(
        self, done: int, total: int, row: BatchPreparedJob
    ) -> None:
        self._prepared.append(row)
        self._append_table_row(row)
        self._tally_label.setText(
            f"Preparing... {done}/{total}: {row.status} -> {row.title}"
        )

    @Slot(object)
    @safe_slot
    def _on_prepare_finished(self, rows) -> None:
        ready = sum(1 for r in self._prepared if r.ready)
        total = len(self._prepared)
        if total == 0:
            self._tally_label.setText(
                "Prepare: no queued jobs at or above the threshold. "
                "Scrape more from the Queue screen, or lower the threshold in Settings."
            )
        else:
            self._tally_label.setText(
                f"Prepare complete: {ready} ready, "
                f"{total - ready} not-ready of {total} considered."
            )
        self._select_all_btn.setEnabled(ready > 0)
        self._deselect_all_btn.setEnabled(total > 0)
        self._update_submit_button()

    @Slot()
    @safe_slot
    def _on_select_all_clicked(self) -> None:
        for cb, row in zip(self._row_checkboxes, self._prepared):
            cb.setChecked(row.ready)

    @Slot()
    @safe_slot
    def _on_deselect_all_clicked(self) -> None:
        for cb in self._row_checkboxes:
            cb.setChecked(False)

    @Slot()
    @safe_slot
    def _on_submit_clicked(self) -> None:
        urls = self._approved_urls()
        if not urls:
            show_error_dialog(
                self,
                "Nothing selected",
                "Tick at least one 'ready' row to submit.",
            )
            return
        live = self._settings.allow_real_submit
        if live:
            ok = confirm_dialog(
                self,
                "Submit LIVE applications?",
                f"You are about to file {len(urls)} REAL Seek applications "
                "under your name. The engine submits one at a time, throttled, "
                "and STOP will halt the batch after the current job. Proceed?",
            )
        else:
            ok = confirm_dialog(
                self,
                "Submit batch (dry-run)?",
                f"This will dry-run {len(urls)} jobs to submit-ready, one at a "
                "time, with throttling. No real applications will be filed.",
            )
        if not ok:
            return
        self._worker.run_batch(
            urls,
            allow_real_submit=live,
            throttle_seconds=self._settings.batch_throttle_seconds,
        )
        self._tally_label.setText(
            f"Running... {len(urls)} jobs. "
            + ("LIVE submission." if live else "Dry-run.")
        )
        self._run_progress.setRange(0, len(urls))
        self._run_progress.setValue(0)
        self._run_progress.setVisible(True)

    @Slot()
    @safe_slot
    def _on_stop_clicked(self) -> None:
        self._worker.stop_batch()
        self._stop_btn.setEnabled(False)
        self._tally_label.setText(
            self._tally_label.text() + "  (STOP requested; finishing current job.)"
        )

    @Slot(int, int, object)
    @safe_slot
    def _on_run_progress(
        self, done: int, total: int, result: ApplicationResult
    ) -> None:
        self._run_progress.setValue(done)
        self._tally_label.setText(self._format_running_tally(done, total))
        # Mark the row in the table if we can find it.
        for r_idx, row in enumerate(self._prepared):
            if row.url == result.job_url:
                self._row_checkboxes[r_idx].setEnabled(False)
                self._update_row_status_text(r_idx, result.status.value)
                break

    @Slot(object)
    @safe_slot
    def _on_run_finished(self, tally: BatchRunResult) -> None:
        self._run_progress.setVisible(False)
        self._stop_btn.setEnabled(False)
        readout = (
            f"Batch {tally.stop_reason}. "
            f"Submitted {tally.submitted}, verified {tally.verified}, "
            f"uncertain {tally.submitted_uncertain} "
            f"(verify on Seek), failed {tally.failed}, "
            f"dry-run-verified {tally.dry_run_verified}, "
            f"skipped {tally.skipped_low_score}, cancelled {tally.cancelled}."
        )
        self._tally_label.setText(readout)

    def _format_running_tally(self, done: int, total: int) -> str:
        # Build a quick running count from the per-job results we have seen
        # so far (not the BatchRunResult, which only arrives at the end).
        # The signal we just received corresponds to the latest result; we
        # rely on the worker emitting in order.
        return f"Running batch... {done}/{total}."

    @Slot(int, int, int, int)
    @safe_slot
    def _on_row_changed(self, cur_row: int, _c: int, _pr: int, _pc: int) -> None:
        if cur_row < 0 or cur_row >= len(self._prepared):
            self._detail_header.setText("Select a row to preview.")
            self._cover_view.clear()
            self._qa_view.clear()
            self._error_view.clear()
            return
        row = self._prepared[cur_row]
        self._detail_header.setText(
            f"{row.title} at {row.company}  --  status: {row.status}"
        )
        # Cover letter from the dataclass; if absent, try sidecar.
        cover_text = row.cover_letter_text
        if cover_text is None and row.cover_pdf is not None:
            for cand in [
                Path(str(row.cover_pdf) + ".txt"),
                self._engine_workdir / Path(str(row.cover_pdf) + ".txt"),
            ]:
                try:
                    if cand.exists():
                        cover_text = cand.read_text(encoding="utf-8")
                        break
                except Exception:
                    pass
        self._cover_view.setPlainText(
            cover_text or "(No cover letter available for this row.)"
        )

        qa = row.screening_answers or []
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

        if row.error_message:
            self._error_view.setPlainText(
                f"{row.exception_type}: {row.error_message}"
            )
        else:
            self._error_view.setPlainText("(No error.)")

    @Slot(str)
    @safe_slot
    def _on_worker_state(self, state: str) -> None:
        running = state in ("running", "cancelling")
        self._prepare_btn.setEnabled(not running)
        self._prepare_cancel_btn.setEnabled(running)
        self._prepare_progress.setVisible(running)
        self._stop_btn.setEnabled(running)
        self._update_submit_button()

    @Slot(str, str)
    @safe_slot
    def _on_worker_failed(self, op: str, msg: str) -> None:
        if op.startswith("batch_"):
            show_error_dialog(self, f"Batch error: {op}", msg)

    # ----------------------------------------------------------- table

    def _append_table_row(self, row: BatchPreparedJob) -> None:
        i = self._table.rowCount()
        self._table.insertRow(i)

        cb = QCheckBox()
        cb.setEnabled(row.ready)
        cb.stateChanged.connect(lambda _s: self._update_submit_button())
        # Center the checkbox inside the cell.
        wrap = QWidget()
        hl = QHBoxLayout(wrap)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setAlignment(Qt.AlignCenter)
        hl.addWidget(cb)
        self._table.setCellWidget(i, 0, wrap)
        self._row_checkboxes.append(cb)

        score_item = QTableWidgetItem(
            "" if row.score is None else f"{int(row.score):>3}"
        )
        score_item.setTextAlignment(Qt.AlignCenter)
        score_item.setBackground(_score_colour(row.score))
        self._table.setItem(i, 1, score_item)
        self._table.setItem(i, 2, QTableWidgetItem(row.title))
        self._table.setItem(i, 3, QTableWidgetItem(row.company))

        status_item = QTableWidgetItem(row.status)
        status_item.setForeground(_status_colour(row.status))
        font = status_item.font()
        font.setBold(True)
        status_item.setFont(font)
        self._table.setItem(i, 4, status_item)
        # URL truncated in column; full visible in tooltip.
        url_short = row.url.replace("https://au.seek.com/job/", "#")
        url_item = QTableWidgetItem(url_short)
        url_item.setToolTip(row.url)
        self._table.setItem(i, 5, url_item)

    def _update_row_status_text(self, idx: int, status: str) -> None:
        item = self._table.item(idx, 4)
        if item is not None:
            item.setText(status)


# --------------------------------------------------------------------------- colours


def _score_colour(score: int | None) -> QColor:
    if score is None:
        return QColor("#f3f4f6")
    if score >= 70:
        return QColor("#bbf7d0")
    if score >= 30:
        return QColor("#fef3c7")
    return QColor("#fee2e2")


def _status_colour(status: str) -> QColor:
    if status == "ready":
        return QColor("#15803d")
    if status == "not_quick_apply":
        return QColor("#c2410c")
    if status == "skipped_low_score":
        return QColor("#6b7280")
    if status == "cancelled":
        return QColor("#374151")
    return QColor("#b91c1c")


# --------------------------------------------------------------------------- style


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


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 8px 16px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #93c5fd; color: #e0e7ff; }"
    )


def _text_view_css() -> str:
    return (
        "QPlainTextEdit { background: #f8fafc; color: #111827; "
        "font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }"
    )
