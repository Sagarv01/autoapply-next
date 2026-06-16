"""Phase F: in-app version floor.

The proxy publishes a minimum client version (and rejects below-floor clients with
426). The app compares its own version to that floor and, if too old, shows an
update-required screen linking to the download. Unknown/dev versions (0.0.0 or
unparseable) never force an update (fail open).
"""

from __future__ import annotations

import pytest

from autoapply_next.version_check import is_update_required


@pytest.mark.parametrize(
    "current,minimum,expected",
    [
        ("1.0.0", "1.2.0", True),    # older minor
        ("1.2.3", "1.2.4", True),    # older patch
        ("0.9.0", "1.0.0", True),    # older major
        ("1.2.0", "1.2.0", False),   # equal
        ("2.0.0", "1.9.9", False),   # newer major
        ("1.3.0", "1.2.9", False),   # newer minor
        ("1.2", "1.2.0", False),     # short form, equal
    ],
)
def test_compares_semver(current, minimum, expected):
    assert is_update_required(current, minimum) is expected


@pytest.mark.parametrize(
    "current,minimum",
    [
        ("", "1.0.0"),        # no current version
        ("1.0.0", ""),        # no floor published
        ("0.0.0", "1.0.0"),   # dev / unknown build -> never forced
        ("garbage", "1.0.0"),
        ("1.0.0", "x.y.z"),
    ],
)
def test_fails_open_on_unknown_versions(current, minimum):
    assert is_update_required(current, minimum) is False
