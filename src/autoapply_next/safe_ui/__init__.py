"""Global error safety net for the GUI.

Three things wired here so no failure is silent:

1. `install_global_handlers()` installs `sys.excepthook` (main-thread Python
   exceptions) and `qInstallMessageHandler` (Qt internal log messages).
   Both route to the structured logger (already PII-scrubbed by
   `safe_logging.scrubber`) AND emit a Qt signal on a process-wide
   `ErrorBus`, which the MainWindow surfaces as a non-fatal dialog. The
   app stays usable after an error; we do not exit.

2. `safe_slot(fn)` wraps a Qt slot so any exception inside it becomes a
   visible error via the same `ErrorBus`. Without this, Qt prints the
   stack to stderr and silently continues; the user sees nothing.

3. `show_error_dialog(parent, title, summary, detail)` is the only place
   that builds an error popup. The "details" expander carries the full
   stack for the user to forward to us if needed.

Together these guarantee that any uncaught exception, anywhere in the
process, surfaces visibly with a readable message.
"""

from .error_handler import (
    ErrorBus,
    confirm_dialog,
    get_bus,
    install_global_handlers,
    safe_slot,
    show_error_dialog,
)

__all__ = [
    "ErrorBus",
    "confirm_dialog",
    "get_bus",
    "install_global_handlers",
    "safe_slot",
    "show_error_dialog",
]
