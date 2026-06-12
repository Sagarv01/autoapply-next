"""Clean-machine bootstrap: frozen-aware engine resolution, workdir seeding,
and dependency preflight. These are the fixes that make a packaged build run
on a machine that is not the developer's, and that seed the live-pilot workdir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from autoapply_next.platform import bootstrap


# ----------------------------------------------------- engine resolution


def test_engine_vendor_dir_dev_points_at_repo_vendor() -> None:
    vendor = bootstrap.engine_vendor_dir()
    assert vendor.name == "job-finder"
    assert vendor.parent.name == "vendor"
    # In this checkout the vendored engine actually exists.
    assert (vendor / "seek_apply.py").exists()


def test_engine_vendor_dir_frozen_uses_meipass(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert bootstrap.engine_vendor_dir() == tmp_path / "vendor" / "job-finder"


def test_ensure_engine_importable_puts_vendor_on_path() -> None:
    vendor = bootstrap.ensure_engine_importable()
    assert str(vendor) in sys.path
    assert (vendor / "seek_apply.py").exists()


# ------------------------------------------------------------ seeding


def test_seed_engine_workdir_makes_empty_workdir_runnable(tmp_path) -> None:
    assert not bootstrap.workdir_is_runnable(tmp_path)
    actions = bootstrap.seed_engine_workdir(tmp_path)
    assert bootstrap.workdir_is_runnable(tmp_path)
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / "sessions" / "seek").is_dir()
    assert (tmp_path / "output" / "dryrun-screenshots").is_dir()
    assert (tmp_path / "errors").is_dir()
    assert any("config.yaml" in a for a in actions)


def test_seed_does_not_overwrite_existing_config(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("candidate:\n  name: Real User\n", encoding="utf-8")
    bootstrap.seed_engine_workdir(tmp_path)
    # The user's real config must be preserved, never clobbered by a template.
    assert "Real User" in cfg.read_text(encoding="utf-8")


def test_seed_force_config_overwrites_from_source(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("candidate:\n  name: Stale\n", encoding="utf-8")
    source = tmp_path / "source.yaml"
    source.write_text("candidate:\n  name: Fresh Pilot\n", encoding="utf-8")
    bootstrap.seed_engine_workdir(
        tmp_path, config_source=source, force_config=True
    )
    assert "Fresh Pilot" in cfg.read_text(encoding="utf-8")


def test_seed_from_explicit_config_source(tmp_path) -> None:
    source = tmp_path / "live.yaml"
    source.write_text("candidate:\n  email: pilot@example.com\n", encoding="utf-8")
    dest = tmp_path / "wd"
    bootstrap.seed_engine_workdir(dest, config_source=source)
    assert "pilot@example.com" in (dest / "config.yaml").read_text(encoding="utf-8")


# ------------------------------------------------------------ preflight


def test_preflight_report_flags_missing_claude(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: None  # nothing on PATH
    )
    monkeypatch.setattr(bootstrap.Path, "exists", lambda self: False)
    problems = bootstrap.preflight_report()
    assert any("claude" in p.lower() for p in problems)
    assert any("soffice" in p.lower() or "libreoffice" in p.lower() for p in problems)


def test_preflight_report_clean_when_deps_present(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    assert bootstrap.preflight_report() == []
