"""Application-wide Qt theme.

The public site uses a light operational SaaS palette: white surfaces,
blue primary actions, neutral borders, small radii, and restrained density.
This QSS keeps that visual language consistent across the PySide app without
coupling workflow screens to presentation details.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication


def apply_app_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)


STYLESHEET = """
* {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 13px;
  color: #111827;
}

QMainWindow,
QWidget {
  background: #ffffff;
}

QToolTip {
  background: #111827;
  color: #ffffff;
  border: 0;
  border-radius: 6px;
  padding: 6px 8px;
}

QStatusBar {
  background: #ffffff;
  border-top: 1px solid #e4e7ed;
}

QToolBar#nav-toolbar {
  background: #f8f9fb;
  border-right: 1px solid #e4e7ed;
  spacing: 4px;
  padding: 12px 10px;
}

QToolBar#nav-toolbar QToolButton {
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  color: #4b5563;
  font-weight: 600;
  padding: 9px 12px;
  text-align: left;
}

QToolBar#nav-toolbar QToolButton:hover {
  background: #eef2ff;
  border-color: #dbeafe;
  color: #1d4ed8;
}

QToolBar#nav-toolbar QToolButton:checked {
  background: #2563eb;
  border-color: #2563eb;
  color: #ffffff;
}

QLabel#brand-mark {
  background: #2563eb;
  color: #ffffff;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 800;
}

QLabel#brand-name {
  color: #111827;
  font-size: 16px;
  font-weight: 800;
}

QLabel#brand-subtitle {
  color: #6b7280;
  font-size: 11px;
  font-weight: 500;
}

QFrame#scrape-row,
QFrame#url-row,
QFrame#status-bar,
QFrame#tally {
  background: #ffffff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
}

QLineEdit,
QPlainTextEdit,
QTextEdit,
QComboBox,
QSpinBox {
  background: #ffffff;
  border: 1px solid #d8dce4;
  border-radius: 7px;
  padding: 8px 10px;
  selection-background-color: #2563eb;
  selection-color: #ffffff;
}

QLineEdit:focus,
QPlainTextEdit:focus,
QTextEdit:focus,
QComboBox:focus,
QSpinBox:focus {
  border: 1px solid #2563eb;
}

QLineEdit:disabled,
QPlainTextEdit:disabled,
QTextEdit:disabled {
  background: #f1f3f7;
  color: #9ca3af;
}

QPushButton {
  background: #ffffff;
  border: 1px solid #d8dce4;
  border-radius: 7px;
  color: #111827;
  font-weight: 700;
  padding: 8px 13px;
}

QPushButton:hover {
  background: #f1f3f7;
  border-color: #bfdbfe;
}

QPushButton:pressed {
  background: #e5e7eb;
}

QPushButton:disabled {
  background: #f3f4f6;
  border-color: #e5e7eb;
  color: #9ca3af;
}

QPushButton[buttonRole="primary"] {
  background: #2563eb;
  border-color: #2563eb;
  color: #ffffff;
}

QPushButton[buttonRole="primary"]:hover {
  background: #1d4ed8;
  border-color: #1d4ed8;
}

QPushButton[buttonRole="success"] {
  background: #16a34a;
  border-color: #16a34a;
  color: #ffffff;
}

QPushButton[buttonRole="danger"] {
  background: #dc2626;
  border-color: #dc2626;
  color: #ffffff;
}

QPushButton[buttonRole="danger"]:hover {
  background: #b91c1c;
  border-color: #b91c1c;
}

QCheckBox {
  color: #374151;
  spacing: 8px;
}

QCheckBox::indicator {
  width: 16px;
  height: 16px;
  border: 1px solid #d8dce4;
  border-radius: 4px;
  background: #ffffff;
}

QCheckBox::indicator:checked {
  background: #2563eb;
  border-color: #2563eb;
}

QTableWidget {
  background: #ffffff;
  alternate-background-color: #f8f9fb;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  gridline-color: #eef0f4;
  selection-background-color: #dbeafe;
  selection-color: #111827;
}

QHeaderView::section {
  background: #f8f9fb;
  border: 0;
  border-bottom: 1px solid #e4e7ed;
  color: #4b5563;
  font-size: 12px;
  font-weight: 800;
  padding: 8px;
}

QTableWidget::item {
  padding: 6px;
}

QTabWidget::pane {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #ffffff;
}

QTabBar::tab {
  background: #f8f9fb;
  border: 1px solid #e4e7ed;
  border-bottom: 0;
  border-top-left-radius: 7px;
  border-top-right-radius: 7px;
  color: #4b5563;
  font-weight: 700;
  padding: 8px 12px;
  margin-right: 4px;
}

QTabBar::tab:selected {
  background: #ffffff;
  color: #2563eb;
}

QProgressBar {
  background: #eef0f4;
  border: 0;
  border-radius: 5px;
  color: #374151;
  height: 10px;
  text-align: center;
}

QProgressBar::chunk {
  background: #2563eb;
  border-radius: 5px;
}

QScrollArea {
  border: 0;
  background: transparent;
}

QSplitter::handle {
  background: #eef0f4;
}

QMessageBox {
  background: #ffffff;
}
"""
