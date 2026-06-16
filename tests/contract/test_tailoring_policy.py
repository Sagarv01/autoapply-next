"""Free/Basic apply with base docs; tailoring is Pro-only.

The client mirrors the server gate: only the Pro tier tailors. A small module-level
policy lets the worker set "should this batch tailor?" once (from the fetched
tier) for the apply flow to read. Default is True so existing flows/tests that
never set it keep tailoring (back-compat); the server gate is still the real
enforcement.
"""

from __future__ import annotations

import pytest

from autoapply_next.engine import tailoring_policy as tp


@pytest.fixture(autouse=True)
def _reset():
    tp.set_tailoring_allowed(True)
    yield
    tp.set_tailoring_allowed(True)


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("pro", True),
        ("Pro", True),
        ("basic", False),
        ("free", False),
        ("starter", False),  # legacy basic
        ("", False),
        (None, False),
    ],
)
def test_allows_tailoring_is_pro_only(tier, expected):
    assert tp.allows_tailoring(tier) is expected


def test_policy_defaults_to_true_for_backcompat():
    # a fresh import / existing flow that never sets it keeps tailoring
    assert tp.tailoring_allowed() is True


def test_set_and_get_policy():
    tp.set_tailoring_allowed(False)
    assert tp.tailoring_allowed() is False
    tp.set_tailoring_allowed(True)
    assert tp.tailoring_allowed() is True
