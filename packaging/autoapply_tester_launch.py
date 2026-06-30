"""Frozen tester-build entry point."""

import os
import sys

os.environ.setdefault("AUTOAPPLY_BUILD_AUDIENCE", "tester")

from autoapply_next.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
