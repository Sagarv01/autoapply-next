"""Test bootstrap.

The vendored engine lives at `vendor/job-finder/`. Tests that import
`seek_apply` etc. (either directly or via the adapter) need the vendor
directory on sys.path AND need cwd-relative resources (`config.yaml`,
`assets/`) reachable. The adapter handles this at runtime via
`_engine_workdir`; tests do it via this conftest.

Pure unit tests of the adapter / safety / hooks modules should NOT depend on
the real engine being importable; they mock `seek_apply` via
`monkeypatch.setitem(sys.modules, ...)`. Tests that do need the real engine
are marked `@pytest.mark.requires_engine`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ENGINE = REPO_ROOT / "vendor" / "job-finder"
SRC_ROOT = REPO_ROOT / "src"


def pytest_configure(config: pytest.Config) -> None:
    """Put src/ on sys.path for autoapply_next imports."""
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    # Register the requires_engine marker so tests can opt in.
    config.addinivalue_line(
        "markers",
        "requires_engine: marks tests that need the real vendored engine "
        "importable (cwd + sys.path setup).",
    )


@pytest.fixture(autouse=True)
def _reset_tailoring_policy():
    """Isolate the process-global tailoring policy between tests.

    `tailoring_policy._allowed` is module-level by design (it deliberately does
    not thread through run_batch -> apply_to_job). A test that sets it False, or
    a worker run that resolves a non-Pro tier, would otherwise leak that state
    into a later test that assumes the default True, silently routing it through
    the base-docs document path. Reset to the default before and after every
    test so each starts from a known state.
    """
    from autoapply_next.engine import tailoring_policy

    tailoring_policy.set_tailoring_allowed(None)
    yield
    tailoring_policy.set_tailoring_allowed(None)


@pytest.fixture
def engine_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a minimal engine workdir: copy a stub config.yaml and assets/
    into a tmp dir, set cwd there, put the vendored engine on sys.path.

    The vendored engine is NOT copied; we just point sys.path at it. The
    workdir holds the runtime-mutable bits (sessions/, output/, jobs.db,
    bot.log, errors/).
    """
    if not VENDOR_ENGINE.exists():
        pytest.skip(f"vendor engine not present at {VENDOR_ENGINE}")

    # Minimal config.yaml.
    (tmp_path / "config.yaml").write_text(
        "candidate:\n"
        "  name: Test User\n"
        "  email: test@example.com\n"
        "  phone: '+61 400 000 000'\n"
        "search:\n"
        "  skills: ['python']\n"
        "  location: Australia\n"
        "  match_threshold: 20\n"
        "scraper:\n"
        "  interval_seconds: 60\n"
        "  interval_jitter_seconds: 0\n"
        "  board_block_pause_minutes: 10\n",
        encoding="utf-8",
    )
    # Minimal assets/profile.txt.
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "profile.txt").write_text(
        "Sagar Verma\nsagarvd130@gmail.com\n+61 491 621 148\n\n"
        "PROFESSIONAL SUMMARY\nSenior platform engineer with 15 years experience.\n"
        "\nCORE SKILLS\nPython, AWS, Terraform\n",
        encoding="utf-8",
    )
    (tmp_path / "sessions" / "seek").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "seek" / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "errors").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)

    # Evict any fake engine modules left in sys.modules by previous tests
    # (notably the synthetic seek_apply built by test_safety_gate.py).
    for mod_name in [
        "seek_apply",
        "applicator",
        "matcher",
        "tailorer",
        "models",
        "claude_cli",
        "utils",
    ]:
        sys.modules.pop(mod_name, None)

    monkeypatch.syspath_prepend(str(VENDOR_ENGINE))
    monkeypatch.chdir(tmp_path)

    yield tmp_path
