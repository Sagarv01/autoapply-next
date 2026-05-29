"""AutoApply Next: PySide6 desktop GUI wrapping the job-finder engine.

The engine is treated as a black-box dependency vendored at `vendor/job-finder/`.
This package provides:

- `engine.adapter`: the one composed entry point `apply_to_job(...)`.
- `engine.safety`: the runtime gate that neutralises the engine's final-submit step
  in dry-run mode, without editing engine source.
- `engine.worker`: the Qt-side worker that runs the engine on its own asyncio loop
  in its own thread and surfaces progress over Qt signals.
- `ui.*`: the seven minimal screens.

Nothing in this package may edit anything under `vendor/`. The adapter imports
engine modules; it never patches them on disk.
"""

__version__ = "0.1.0"
