"""Frozen-build entry point.

PyInstaller runs the entry script as the top-level ``__main__`` module with no
package context, so ``autoapply_next/__main__.py`` cannot be the entry directly:
its relative imports (``from .platform...``) raise
``ImportError: attempted relative import with no known parent package``.

This launcher imports the package by its absolute name and calls ``main()``.
Because ``autoapply_next.__main__`` is then imported AS a submodule of the
package, its relative imports resolve normally. Used in both dev
(``python packaging/autoapply_launch.py``) and the frozen bundle.
"""

import sys

from autoapply_next.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
