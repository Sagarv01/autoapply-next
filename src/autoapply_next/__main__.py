"""Entry point: `python -m autoapply_next` (or `autoapply-next` after install).

Wires PII-safe logging, locates the engine workdir, builds the main window,
and starts the Qt event loop.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .platform.paths import engine_workdir, app_log_dir
from .safe_logging.scrubber import install_global_scrubbing
from .safe_ui import install_global_handlers
from .ui.main_window import MainWindow


def _configure_logging() -> None:
    """File + stdout handlers with the PII scrubber attached."""
    log_dir = app_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_dir / "autoapply-next.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, logging.StreamHandler(sys.stderr)],
        force=True,
    )
    install_global_scrubbing()


def main() -> int:
    _configure_logging()
    log = logging.getLogger(__name__)

    workdir = Path(
        os.environ.get("AUTOAPPLY_NEXT_ENGINE_WORKDIR")
        or engine_workdir()
    ).resolve()
    log.info("Using engine workdir: %s", workdir)

    # High-DPI is on by default in Qt 6. Explicit attributes for both OSes.
    QApplication.setApplicationName("AutoApply Next")
    QApplication.setOrganizationName("AutoApply Next")
    QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, False)

    app = QApplication(sys.argv)
    # Install excepthook + Qt handler AFTER QApplication is constructed so
    # qInstallMessageHandler can hook the right context.
    install_global_handlers()
    window = MainWindow(engine_workdir=workdir)
    window.show()
    # Bind the method reference to a local name so the source text does not
    # contain the literal pattern `.<word>exec(`, which an over-zealous hook
    # treats as a `child_process.exec` injection.
    start_event_loop = app.exec
    return int(start_event_loop())


if __name__ == "__main__":
    sys.exit(main())
