"""Verifier hardening: tighten the title+company fallback so a short, generic
target cannot fuzzy-match an unrelated already-applied card and flip a FAILED
submit to a false APPLIED (a silent pass, which the autonomous gate forbids).

The win is on the title axis: a single-token target must match exactly, not by
being a substring of a longer card title. Multi-token truncation tolerance and
company-suffix variance are preserved (those are real, safe matches).
"""

from __future__ import annotations

from autoapply_next.engine.verifier import title_company_match


# ----------------------------------------------------- adversarial: must reject


def test_rejects_single_token_title_substring_collision() -> None:
    # The recon example: a different applied card ("Software Engineer @
    # Hydrogen Group") must NOT verify a failed "Engineer @ Hydrogen" submit.
    assert not title_company_match(
        "Software Engineer", "Hydrogen Group",  # an unrelated applied card
        "Engineer", "Hydrogen",                 # our (failed) target
    )


def test_rejects_generic_single_word_title_collision() -> None:
    assert not title_company_match(
        "Senior Data Analyst", "Acme",
        "Analyst", "Acme",
    )


def test_rejects_when_company_differs_even_if_title_exact() -> None:
    assert not title_company_match(
        "Data Engineer", "Globex",
        "Data Engineer", "Acme",
    )


# ------------------------------------------------ legitimate: must still match


def test_allows_exact_single_token_title() -> None:
    # A genuinely single-word role still verifies when the card shows it
    # exactly (with a normalized 'via Seek' company suffix).
    assert title_company_match(
        "Engineer", "Hydrogen Group via Seek",
        "Engineer", "Hydrogen Group",
    )


def test_allows_multiword_truncation_and_company_suffix() -> None:
    # Card title truncated, company carries an extra legal suffix: still a
    # real match via multi-token overlap.
    assert title_company_match(
        "Senior Data Engineer", "Acme Pty Ltd",
        "Data Engineer", "Acme",
    )


def test_allows_card_truncated_into_target() -> None:
    # Card shows a truncated multi-token title that is a subset of the target.
    assert title_company_match(
        "Senior DevOps...", "Acme",
        "Senior DevOps Cloud Architect", "Acme",
    )


# ---------------------- company stop-list (filler tokens) hardening


def test_rejects_filler_only_company_collision() -> None:
    # A bare filler company ("Group") must NOT subset-match a real "<X> Group"
    # card and flip a failed submit to APPLIED. (Before the stop-list this
    # matched: 'group' was a 1-token subset of 'datacom group'.)
    assert not title_company_match(
        "Data Engineer", "Datacom Group",  # an unrelated applied card
        "Data Engineer", "Group",          # our (failed) target: filler-only company
    )


def test_matches_same_employer_across_different_suffixes() -> None:
    # The SAME employer under different legal suffixes must still match on the
    # meaningful token ('datacom'). (Before the stop-list this FAILED: 'datacom
    # group' tokens were not a subset of 'datacom pty ltd'.)
    assert title_company_match(
        "Data Engineer", "Datacom Pty Ltd",
        "Data Engineer", "Datacom Group",
    )


def test_filler_token_in_both_does_not_alone_match() -> None:
    # Two genuinely different employers that share only a filler token must NOT
    # match on that filler alone.
    assert not title_company_match(
        "Data Engineer", "Acme Technologies",
        "Data Engineer", "Globex Technologies",
    )
