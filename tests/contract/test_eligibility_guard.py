"""EligibilityAnswerGuard: answer a work-rights dropdown truthfully for a 485
holder, or hold it. Never claim a status or visa type the candidate does not hold.

Two false-claim bugs were observed live on the Seek AU_Q_6 right-to-work dropdown,
both from the engine's rule path (seek_apply._answer_field), never the LLM:
  1. the fuzzy matcher picked "I'm an Australian citizen", and
  2. after a first fix, it picked "I have a family/partner visa with no
     restrictions" (matched "no restrictions") for a 485 temporary-graduate holder.
Both are false. The guard wraps _answer_field: for an eligibility dropdown it picks
the option consistent with the candidate's actual visa (485 / temporary visa with
work rights), never a citizen/PR/other-visa-type option, and HOLDS the question for
the user when no truthful option exists rather than guessing.
"""

from __future__ import annotations

import sys
import types

import pytest

from autoapply_next.engine.eligibility_guard import (
    EligibilityAnswerGuard,
    is_eligibility_option_set,
    is_eligibility_question,
    safe_eligibility_pick,
)
from autoapply_next.screening.interceptor import QuestionHeldError

VISA = "485 Temporary Graduate Visa"

# The real AU_Q_6 dropdown, as DOM option dicts (incl. the family/partner trap).
AU_Q_6 = [
    {"text": "I'm an Australian citizen", "value": "c"},
    {"text": "I'm a permanent resident", "value": "pr"},
    {"text": "I'm a New Zealand citizen", "value": "nz"},
    {"text": "I have a family/partner visa with no restrictions", "value": "fp"},
    {"text": "I have a temporary visa that allows me to work in Australia", "value": "tv"},
    {"text": "I'm not currently entitled to work in Australia", "value": "ne"},
]
AU_Q_6_TEXTS = [o["text"] for o in AU_Q_6]


# ---- safe_eligibility_pick --------------------------------------------------


def test_picks_temporary_work_visa_not_citizen_or_family():
    pick = safe_eligibility_pick(AU_Q_6_TEXTS, VISA)
    assert pick == "I have a temporary visa that allows me to work in Australia"


def test_never_picks_family_partner_even_with_no_restrictions():
    pick = safe_eligibility_pick(AU_Q_6_TEXTS, VISA)
    assert "family" not in pick.lower() and "partner" not in pick.lower()


def test_prefers_explicit_485_or_graduate_option():
    opts = [
        "I'm an Australian citizen",
        "Subclass 485 (Temporary Graduate)",
        "I have a temporary visa that allows me to work in Australia",
    ]
    assert "485" in safe_eligibility_pick(opts, VISA)


def test_holds_on_other_when_no_truthful_visa_option():
    # "Other" is ambiguous; under strict exact-match we hold rather than guess.
    opts = ["I'm an Australian citizen", "I'm a permanent resident", "Other"]
    assert safe_eligibility_pick(opts, VISA) is None


def test_none_when_only_disqualified_or_wrong_visa_types():
    opts = [
        "I'm an Australian citizen",
        "I'm a permanent resident",
        "I have a family/partner visa with no restrictions",
        "I have a student visa",
        "I'm on a working holiday visa",
    ]
    assert safe_eligibility_pick(opts, VISA) is None


def test_none_on_empty():
    assert safe_eligibility_pick([], VISA) is None
    assert safe_eligibility_pick(None, VISA) is None


def test_rejects_restricted_temporary_visa_option():
    # A temporary visa WITH work restrictions is false for a full-work-rights 485.
    opts = [
        "I'm an Australian citizen",
        "I have a temporary visa that has work restrictions",
    ]
    assert safe_eligibility_pick(opts, VISA) is None


def test_is_eligibility_question_matches_and_ignores():
    assert is_eligibility_question("What is your right to work in Australia?")
    assert is_eligibility_question("Are you an Australian citizen?")
    assert not is_eligibility_question("How many years of Python experience?")


# ---- the _answer_field guard ------------------------------------------------


def _fake_seek_apply(answer_field_calls, select_calls, visa=VISA):
    m = types.ModuleType("seek_apply")
    m.CAND_VISA_LABEL = visa  # type: ignore[attr-defined]

    async def _answer_field(page, q, job, candidate):
        answer_field_calls.append(q)

    async def _safe_select_option(page, field_id, desired_value=None, desired_text=None):
        select_calls.append({"field_id": field_id, "value": desired_value, "text": desired_text})
        return True

    m._answer_field = _answer_field  # type: ignore[attr-defined]
    m._safe_select_option = _safe_select_option  # type: ignore[attr-defined]
    return m


class _Job:
    url = "https://au.seek.com/job/92765430"
    title = "Engineer"
    company = "Acme"


def _q(tag="SELECT", label="What are your working rights in Australia?", options=AU_Q_6, fid="AU_Q_6"):
    return {"tag": tag, "label": label, "id": fid, "options": options}


async def test_guard_selects_truthful_visa_option(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", _q(), _Job(), {})
    # selected the temporary-work-visa option by its value; did NOT run the rule path
    assert sel == [{"field_id": "AU_Q_6", "value": "tv",
                    "text": "I have a temporary visa that allows me to work in Australia"}]
    assert af == []


async def test_guard_holds_when_no_truthful_option(monkeypatch):
    af: list = []
    sel: list = []
    bad = [
        {"text": "I'm an Australian citizen", "value": "c"},
        {"text": "I'm a permanent resident", "value": "pr"},
        {"text": "I have a student visa", "value": "s"},
    ]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._answer_field("page", _q(options=bad), _Job(), {})
    assert sel == []  # nothing selected
    assert af == []   # rule path never ran


async def test_guard_delegates_non_eligibility_dropdown(monkeypatch):
    af: list = []
    sel: list = []
    exp = [{"text": "1 year", "value": "1"}, {"text": "4 years", "value": "4"}]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(label="How many years of experience?", options=exp, fid="Q_EXP")
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert len(af) == 1 and sel == []  # delegated to the original rule path


async def test_guard_delegates_non_select(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = {"tag": "INPUT", "label": "Are you an Australian citizen?", "id": "x", "options": []}
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert len(af) == 1 and sel == []


def test_install_restores(monkeypatch):
    fake = _fake_seek_apply([], [])
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._answer_field
    g = EligibilityAnswerGuard()
    g.install()
    assert fake._answer_field is not original
    g.uninstall()
    assert fake._answer_field is original


# ---- strict canonical exact-match for the live "graduate temporary work visa" ----
#
# Observed live on job 92766894: the guard HELD this 11-option right-to-work dropdown
# even though "I have a graduate temporary work visa" is the truthful 485 answer, because
# no fuzzy token matched ("graduate temporary" != "temporary graduate", "...work visa" !=
# "work right"). The fix selects the truthful 485 option via a STRICT canonical exact-match
# (after normalising the "I have a ..." lead-in and trailing parentheticals) -- never a
# loose substring, so it can never land on the near-collision "holiday temporary work visa".

# The real dropdown, verbatim from the run log.
LIVE_RIGHT_TO_WORK = [
    "I'm an Australian citizen",
    "I'm a permanent resident and/or NZ citizen",
    "I have a family/partner visa with no restrictions",
    "I have a graduate temporary work visa",
    "I have a holiday temporary work visa",
    "I have a temporary visa with restrictions on work location (e.g. skilled regional visa 491)",
    "I have a temporary protection or safe haven enterprise work visa",
    "I have a temporary visa with no restrictions (e.g. doctoral student)",
    "I have a temporary visa with restrictions on work hours (e.g. student visa, retirement visa)",
    "I have a temporary visa with restrictions on industry (e.g. temporary activity visa 408)",
    "I require sponsorship to work for a new employer (e.g. 482, 457)",
]


def test_picks_graduate_temporary_work_visa_on_live_dropdown():
    # The truthful 485 option must be selected, not held.
    pick = safe_eligibility_pick(LIVE_RIGHT_TO_WORK, VISA)
    assert pick == "I have a graduate temporary work visa"


def test_never_picks_holiday_temporary_work_visa_near_collision():
    # "holiday temporary work visa" shares "temporary work visa" with the truthful option
    # but is a different visa the candidate does not hold.
    pick = safe_eligibility_pick(LIVE_RIGHT_TO_WORK, VISA)
    assert pick is not None and "holiday" not in pick.lower()


def test_holds_when_graduate_absent_and_only_holiday_or_disqualifiers():
    # With no graduate option present, a holiday visa must never be claimed -> hold.
    opts = [
        "I'm an Australian citizen",
        "I have a holiday temporary work visa",
        "I require sponsorship to work for a new employer (e.g. 482, 457)",
    ]
    assert safe_eligibility_pick(opts, VISA) is None


def test_exact_match_normalises_first_person_prefix_and_parenthetical():
    assert (
        safe_eligibility_pick(
            ["I'm an Australian citizen", "I have a graduate temporary work visa"], VISA
        )
        == "I have a graduate temporary work visa"
    )
    assert (
        safe_eligibility_pick(
            ["I'm an Australian citizen", "Temporary Graduate visa (subclass 485)"], VISA
        )
        == "Temporary Graduate visa (subclass 485)"
    )


# ---- adversarial regression battery (from the verify-eligibility-guard audit) ----
#
# An adversarial sweep empirically reproduced false selections through the old fuzzy
# ladder and a normalization collision. Each must now HOLD (return None); the truthful
# standard-format options must still select. Selecting a FALSE option is the cardinal sin.

WRONG_SUBCLASS = [
    "I hold a subclass 491 visa with full work rights",
    "I am on a Temporary Activity visa (subclass 408) with work rights",
    "I hold a subclass 500 visa that allows me to work",
    "I hold a subclass 417 visa with the right to work",
    "I am on a Prospective Marriage visa (300) with right to work",
    "I hold a subclass 482 visa with the right to work",
]


@pytest.mark.parametrize("bad", WRONG_SUBCLASS)
def test_holds_on_wrong_subclass_number(bad):
    pick = safe_eligibility_pick(["I am an Australian citizen", bad], VISA)
    assert pick is None


OFFICIAL_NAMES = [
    "I hold a Skilled Work Regional (subclass 491) visa which allows me to work",
    "I hold a Temporary Protection visa with the right to work",
    "I am on a Safe Haven Enterprise visa with work rights",
    "I am on a Prospective Marriage visa with the right to work",
]


@pytest.mark.parametrize("bad", OFFICIAL_NAMES)
def test_holds_on_alternate_official_visa_names(bad):
    assert safe_eligibility_pick(["I am an Australian citizen", bad], VISA) is None


def test_australian_national_is_disqualified():
    assert safe_eligibility_pick(
        ["I am an Australian national (full work rights)", "I require employer sponsorship"], VISA
    ) is None
    assert safe_eligibility_pick(
        ["I am a national of Australia", "I require sponsorship"], VISA
    ) is None


PAREN_TRUTHFLIP = [
    "I have a graduate temporary work visa (expired)",
    "I have a graduate visa (ceased)",
    "I have a graduate work visa (UK Tier 5, not Australian)",
    "I have a post-study work visa (in another country, not Australia)",
    "I hold a graduate visa (subclass 600 tourist)",
    "I am on a graduate temporary work visa (Vietnam)",  # NBSP in the core
]


@pytest.mark.parametrize("bad", PAREN_TRUTHFLIP)
def test_truthflipping_parenthetical_does_not_collide(bad):
    # A non-485-affirming parenthetical must never be stripped into a canonical match.
    assert safe_eligibility_pick(["I am an Australian citizen", bad], VISA) is None


NEGATED_NO_RIGHTS = [
    "I do not have the right to work in Australia without sponsorship",
    "I do not currently have the right to work in Australia",
    "I have no current right to work in Australia",
    "I currently have no automatic right to work and must apply",
    "I have the right to work only if my employer sponsors me",
    "Other visa - I require employer sponsorship",
]


@pytest.mark.parametrize("bad", NEGATED_NO_RIGHTS)
def test_negated_or_conditional_rights_never_selected(bad):
    pick = safe_eligibility_pick(["I am an Australian citizen", bad], VISA)
    assert pick is None


def test_negated_pr_clause_never_yields_a_false_no_rights_claim():
    opts = [
        "I am an Australian citizen",
        "I am a permanent resident",
        "I have the right to work in Australia but am not a citizen or permanent resident",
        "I do not have the right to work in Australia",
    ]
    pick = safe_eligibility_pick(opts, VISA)
    # Holding is fine; selecting the "do not have the right" option is never acceptable.
    assert pick != "I do not have the right to work in Australia"
    assert pick is None or "do not" not in pick.lower()


@pytest.mark.parametrize(
    "options",
    [
        ["I am a national of Australia", "I have a graduate temporary work visa"],
        ["I was born in Australia", "I have a graduate temporary work visa"],
        ["Yes, I am settled here permanently", "I have a graduate temporary work visa"],
        ["I am a lawful permanent settler", "I have a graduate temporary work visa"],
    ],
)
def test_detection_engages_on_keywordless_status_option_sets(options):
    # The guard must engage (not delegate to the raw rule path) when the option set
    # encodes a citizenship/nationality/settlement status, even with a bland label.
    assert is_eligibility_option_set(options) is True


# ---- the truthful standard-format options must STILL select ----

STANDARD_TRUTHFUL = [
    ("I have a graduate temporary work visa", "I have a graduate temporary work visa"),
    ("Temporary Graduate visa (subclass 485)", "Temporary Graduate visa (subclass 485)"),
    (
        "I have a temporary visa that allows me to work in Australia",
        "I have a temporary visa that allows me to work in Australia",
    ),
]


@pytest.mark.parametrize("option,expected", STANDARD_TRUTHFUL)
def test_canonical_happy_paths_still_select(option, expected):
    assert safe_eligibility_pick(["I am an Australian citizen", option], VISA) == expected


# ---- round-2 audit: affirming-parenthetical that ALSO carries a truth-flip ----
#
# A trailing "(...485...)" parenthetical is stripped to reach the canonical core, but only
# when it does NOT also carry an expiry/cancellation/negation. "(485, expired)" must HOLD.

FLIP_IN_AFFIRMING_PAREN = [
    "I have a graduate temporary work visa (485, expired)",
    "I have a graduate temporary work visa (485 - now expired)",
    "I have a graduate temporary work visa (485 expired)",
    "I have a graduate visa (cancelled 485)",
    "I have a post-study work visa (485 application refused)",
    "I have a graduate work visa (graduate visa for Canada)",
    "I have a graduate temporary work visa (graduate, work rights ended)",
    "I have a post-study work visa (post-study, not yet granted)",
    "I have a graduate temporary work visa (485 no longer valid)",
    "I have a graduate visa (graduate - awaiting decision)",
]


@pytest.mark.parametrize("bad", FLIP_IN_AFFIRMING_PAREN)
def test_affirming_parenthetical_with_flip_term_holds(bad):
    pick = safe_eligibility_pick(["I am an Australian citizen", "I am a permanent resident", bad], VISA)
    assert pick is None


def test_flip_option_not_manufactured_when_it_is_the_only_survivor():
    # Even when every sibling is disqualified, never manufacture a false expired-visa claim.
    opts = ["I do not have the right to work", "Graduate visa (485 visa has expired)"]
    assert safe_eligibility_pick(opts, VISA) is None


# ---- round-2 audit: detection must engage on any visa-shaped option set ----

VISA_SHAPED_NO_DISQUALIFY = [
    ["Yes - subclass 491 with full work rights", "Yes - graduate temporary work visa", "No"],
    ["Family visa with no restrictions", "Graduate temporary work visa", "Prefer not to answer"],
    ["Skilled Work Regional", "Graduate temporary work visa", "None of the above"],
    ["I can start immediately on my partner visa", "I will need time to arrange documents"],
]


@pytest.mark.parametrize("opts", VISA_SHAPED_NO_DISQUALIFY)
def test_detection_engages_on_visa_shaped_option_sets(opts):
    assert is_eligibility_option_set(opts) is True


def test_detection_ignores_non_visa_dropdowns():
    # Guard against over-engagement: experience / salary / notice / employment-type /
    # seniority dropdowns must NOT look eligible (else the guard would hold them).
    assert is_eligibility_option_set(["1 year", "4 years", "10+ years"]) is False
    assert is_eligibility_option_set(["$80,000", "$100,000", "$120,000"]) is False
    assert is_eligibility_option_set(["Immediately", "2 weeks", "1 month"]) is False
    assert is_eligibility_option_set(["He/Him", "She/Her", "They/Them"]) is False
    assert is_eligibility_option_set(["Permanent", "Contract", "Casual"]) is False
    assert is_eligibility_option_set(["Graduate", "Mid-level", "Senior"]) is False
    assert is_eligibility_option_set(["Full-time", "Part-time"]) is False


async def test_guard_holds_on_partner_visa_only_affirmative(monkeypatch):
    af: list = []
    sel: list = []
    opts = [
        {"text": "I can start immediately on my partner visa", "value": "p"},
        {"text": "I will need time to arrange documents", "value": "t"},
    ]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(label="Tell us about your ability to start in this role.", options=opts, fid="START")
    with EligibilityAnswerGuard():
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert sel == [] and af == []  # never selected the partner option, never delegated


async def test_guard_never_picks_wrong_subclass_on_bypass_label(monkeypatch):
    af: list = []
    sel: list = []
    opts = [
        {"text": "Yes - subclass 491 with full work rights", "value": "491"},
        {"text": "Yes - graduate temporary work visa", "value": "g"},
        {"text": "No", "value": "n"},
    ]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(label="Are you able to commence employment without restriction?", options=opts, fid="COMMENCE")
    with EligibilityAnswerGuard():
        try:
            await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
        except QuestionHeldError:
            pass
    assert af == []  # engaged, never delegated to the raw rule path
    assert all(s["value"] != "491" for s in sel)  # never the false 491 option


# ---- round-3 audit: a work-restriction hidden in an affirming parenthetical ----
#
# A 485 has FULL work rights. An affirming "(...485/graduate...)" paren that ALSO carries a
# restriction (hours cap / part-time / employer-specific / condition 81xx / casual) must
# NOT be stripped into a canonical core. Whitelist stripping: strip only when every word in
# the paren is a benign clarifier (subclass/visa/graduate/...).

RESTRICTION_IN_AFFIRMING_PAREN = [
    "temporary visa with work rights (graduate, 20 hrs limited)",
    "temporary visa with full work rights (graduate, part-time only)",
    "temporary visa with work rights (graduate, employer-specific)",
    "temporary work visa (485, restricted to 24 hours)",
    "temporary visa that allows me to work (graduate, condition 8104)",
    "temporary work visa (graduate, casual only)",
]


@pytest.mark.parametrize("bad", RESTRICTION_IN_AFFIRMING_PAREN)
def test_restriction_in_affirming_parenthetical_holds(bad):
    assert safe_eligibility_pick(["I am an Australian citizen", bad], VISA) is None


def test_benign_clarifier_parenthetical_still_strips_and_selects():
    # A pure-485 clarifier paren still selects truthfully (no regression).
    assert (
        safe_eligibility_pick(["I am an Australian citizen", "Temporary Graduate visa (subclass 485)"], VISA)
        == "Temporary Graduate visa (subclass 485)"
    )


def test_paren_listing_a_non_485_subclass_holds():
    # A paren co-listing a non-485 subclass (even with 485) is ambiguous -> hold.
    assert (
        safe_eligibility_pick(
            ["I am an Australian citizen",
             "I have a temporary visa that allows me to work in Australia (e.g. subclass 485, 482)"],
            VISA,
        )
        is None
    )


# ---- round-3 audit: permanence / authorization detection bypass ----

PERMANENCE_BYPASS = [
    ("Are you able to work in this location without any restriction?",
     ["Yes, indefinitely and without limitation", "On a temporary basis", "No"]),
    ("What is your employment authorization category?",
     ["Authorized to work permanently", "Graduate temporary work visa", "None"]),
    ("How long can you stay and work in this country?",
     ["Permanently, with no end date", "Until my visa expires", "Unsure"]),
    ("Are you eligible for ongoing employment with us?",
     ["Yes, I can work here permanently with no time limit", "Only short-term", "No"]),
]


@pytest.mark.parametrize("label,opts", PERMANENCE_BYPASS)
def test_permanence_dropdowns_are_detected(label, opts):
    assert is_eligibility_question(label) or is_eligibility_option_set(opts)


@pytest.mark.parametrize("label,opts", PERMANENCE_BYPASS)
async def test_guard_never_claims_permanent_on_bypass(monkeypatch, label, opts):
    af: list = []
    sel: list = []
    options = [{"text": t, "value": str(i)} for i, t in enumerate(opts)]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(label=label, options=options, fid="PERM")
    with EligibilityAnswerGuard():
        try:
            await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
        except QuestionHeldError:
            pass
    assert af == []  # guard engaged; never delegated to the raw vendor path
    for s in sel:
        t = s.get("text", "").lower()
        assert "permanent" not in t and "indefinit" not in t  # never a permanent claim


def test_negation_prefix_is_not_stripped_into_a_claim():
    # "No, I have a graduate visa" must never normalise to a canonical selection.
    assert safe_eligibility_pick(
        ["Yes, I am an Australian citizen", "No, I have a graduate visa"], VISA
    ) is None


# ---- round-4 audit: a non-485 subclass number smuggled alongside 485 ----
#
# _ok rejects ANY non-485 visa subclass number even when 485 co-occurs, so an umbrella
# option that conflates 485 with 491/482/... is held rather than claimed.

NUMBER_SMUGGLE = [
    "I have a temporary visa with work rights (subclass 485 or 491)",
    "I have a temporary work visa (subclass 485, 482)",
    "I have a temporary work visa (subclass 485 and 491)",
    "I have a temporary visa with full work rights (subclass 491 / 485)",
    "I have a temporary work visa (graduate, subclass 485, 482)",
]


@pytest.mark.parametrize("bad", NUMBER_SMUGGLE)
def test_non_485_subclass_number_alongside_485_holds(bad):
    pick = safe_eligibility_pick(
        ["I am an Australian citizen or permanent resident", bad], VISA
    )
    assert pick is None


def test_bare_subclass_485_parenthetical_still_selects():
    assert (
        safe_eligibility_pick(["I am an Australian citizen", "I have a temporary work visa (subclass 485)"], VISA)
        == "I have a temporary work visa (subclass 485)"
    )


# ---- round-4 audit: colloquial citizenship/locality (detection must >= rejection) ----

COLLOQUIAL_CITIZENSHIP = [
    ["I am an Aussie national", "I would need help to relocate", "Prefer not to say"],
    ["Australian by birth", "Still deciding", "Not sure"],
    ["I am a permanent local", "No comment", "Other"],
    ["A settled local with no time restrictions", "Maybe", "Ask me later"],
]


@pytest.mark.parametrize("opts", COLLOQUIAL_CITIZENSHIP)
def test_colloquial_citizenship_engages_detection(opts):
    # Detection must never be narrower than rejection: anything _ok would reject must
    # also engage the guard, so it HOLDS rather than delegating to the raw vendor path.
    assert is_eligibility_option_set(opts) is True


def test_colloquial_citizenship_never_selected():
    opts = ["I am an Aussie national", "Australian by birth", "A settled local with no time restrictions"]
    assert safe_eligibility_pick(opts, VISA) is None


# ---- round-5 audit: citizenship-by-proxy vocabulary (passport / naturalised / ...) ----

CITIZENSHIP_PROXY = [
    ["I hold an Australian passport", "I hold a foreign passport and a study-related stay"],
    ["I am a naturalised Australian", "I am here on a graduate scheme after my studies"],
    ["I am an Australian by descent", "I am on a temporary study-related stay"],
    ["I am domiciled in Australia", "I am a dual national", "I have a graduate temporary work visa"],
    ["I can live and work here for good", "I am here for a temporary study-related stay"],
]


@pytest.mark.parametrize("opts", CITIZENSHIP_PROXY)
def test_citizenship_proxy_engages_detection(opts):
    assert is_eligibility_option_set(opts) is True


def test_citizenship_proxy_never_selected():
    # Australian-passport / naturalised / by-descent / "for good" are false for a 485.
    assert safe_eligibility_pick(
        ["I hold an Australian passport", "I am a naturalised Australian",
         "I can live and work here for good"], VISA
    ) is None


# ---- round-5 audit: coverage of common truthful 485 phrasings (no more over-hold) ----

NOW_SELECTABLE = [
    "I have full working rights in Australia",
    "I have full work rights in Australia",
    "I have the right to work in Australia",
    "I am eligible to work in Australia",
    "I hold a Subclass 485 visa",
    "Visa with unrestricted work rights",
    "I have a temporary visa with no work restrictions",
]


@pytest.mark.parametrize("good", NOW_SELECTABLE)
def test_common_truthful_485_phrasings_select(good):
    assert safe_eligibility_pick(["I am an Australian citizen or permanent resident", good], VISA) == good


# ---- round-6 audit: paren number/short-word escapes (only exact clarifiers strip) ----

PAREN_ESCAPES = [
    "Temporary visa with full work rights (subclass 4850)",   # 4-digit, 485 is a substring
    "Temporary visa with full work rights (visa 1485)",
    "I have a temporary work visa (graduate stream, subclass 49 1)",   # split 491
    "full work rights (graduate 49 1)",
    "temporary work visa (graduate stream, 41 7)",            # split 417
    "full work rights (no graduate visa)",                    # short-word negation
    "full work rights (ex graduate)",
]


@pytest.mark.parametrize("bad", PAREN_ESCAPES)
def test_paren_number_and_shortword_escapes_hold(bad):
    assert safe_eligibility_pick(["I am an Australian citizen", "I require visa sponsorship", bad], VISA) is None


def test_exact_clarifier_parentheticals_still_strip():
    assert (
        safe_eligibility_pick(["I am an Australian citizen", "Temporary Graduate visa (subclass 485)"], VISA)
        == "Temporary Graduate visa (subclass 485)"
    )
    assert "485" in (safe_eligibility_pick(
        ["I am an Australian citizen", "Subclass 485 (Temporary Graduate)"], VISA
    ) or "")


# ---- round-6 audit: informal visa-synonym detection ----

INFORMAL_SYNONYMS = [
    ["I am here on a backpacker arrangement", "I just graduated", "Prefer not to say"],
    ["I am on a bridging arrangement", "I am on a regional scheme", "Other"],
    ["My stay depends on my employer", "I can only work part of the year", "Unsure"],
]


@pytest.mark.parametrize("opts", INFORMAL_SYNONYMS)
def test_informal_visa_synonyms_engage_detection(opts):
    assert is_eligibility_option_set(opts) is True


# ---- round-7 coverage: the common "not a citizen/PR but [right to work]" compound ----

def test_compound_not_citizen_but_right_to_work_selects():
    opts = [
        "I'm an Australian citizen",
        "I am a permanent resident / NZ citizen",
        "I am not a citizen/PR but have the right to work in Australia",
    ]
    assert (
        safe_eligibility_pick(opts, VISA)
        == "I am not a citizen/PR but have the right to work in Australia"
    )


@pytest.mark.parametrize("opt", [
    "I am not a citizen but I have the right to work in Australia",
    "Not a citizen/PR but eligible to work in Australia",
])
def test_compound_negated_citizenship_variants_select(opt):
    assert safe_eligibility_pick(["I'm an Australian citizen", opt], VISA) == opt


def test_spelled_out_australian_citizen_negation_holds_safely():
    # The spelled-out "not an Australian citizen but ..." form is rejected by _ok on the
    # 'australian citizen' substring. Over-hold (safe) -- we do NOT add fragile negation
    # parsing to _ok; the important property is it never selects a false option.
    pick = safe_eligibility_pick(
        ["I'm an Australian citizen",
         "I am not an Australian citizen but I have the right to work in Australia"], VISA,
    )
    assert pick is None


@pytest.mark.parametrize("bad", [
    "I am not a citizen but I require sponsorship",
    "I am not a citizen but I am on a student visa",
    "I am not a citizen but I hold a subclass 491 visa",
])
def test_negated_citizenship_with_a_false_tail_still_holds(bad):
    # Stripping the (true) "not a citizen" prefix must never let a false tail through;
    # _ok screens the full original string, so these all hold.
    assert safe_eligibility_pick(["I'm an Australian citizen", bad], VISA) is None


ADDITIVE_COVERAGE = [
    "I am authorised to work in Australia",
    "I am authorized to work in Australia",
    "I am legally allowed to work in Australia",
    "I currently have the right to work in Australia",
    "I have work rights in Australia",
    "I am permitted to work in Australia",
]


@pytest.mark.parametrize("good", ADDITIVE_COVERAGE)
def test_additive_coverage_phrasings_select(good):
    assert safe_eligibility_pick(["I am an Australian citizen", good], VISA) == good


# ---- round-8 audit: standalone citizenship synonyms + the selection chokepoint ----

STANDALONE_CITIZENSHIP = [
    ["I am Australian", "I am free to take up employment here", "I cannot start yet"],
    ["Yes, I was born and raised here", "Yes, I am a local", "No, I am here short-term"],
    ["I have settled status", "I have leave to remain here", "I have a short-term arrangement"],
]


@pytest.mark.parametrize("opts", STANDALONE_CITIZENSHIP)
def test_standalone_citizenship_engages_detection(opts):
    assert is_eligibility_option_set(opts) is True


def test_standalone_citizenship_never_selected():
    assert safe_eligibility_pick(
        ["I am Australian", "I was born and raised here", "I have settled status"], VISA
    ) is None


async def test_selection_chokepoint_refuses_false_status_text(monkeypatch):
    # Defense-in-depth: even if a status dropdown bypasses detection and the vendor path
    # tries to SELECT a false-status option, the chokepoint vetoes it and HOLDS.
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._safe_select_option("page", "FID", desired_text="I am Australian")
    assert sel == []  # the real selection was never performed


async def test_selection_chokepoint_allows_truthful_text(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option(
            "page", "FID", desired_text="I have a graduate temporary work visa"
        )
    assert len(sel) == 1


ROUND8_COVERAGE = [
    "Yes, I hold a visa with full work rights",
    "I have the right to work in Australia with no restrictions",
    "I have unrestricted working rights",
    "I have a visa that allows me to work",
    "Temporary resident with work rights",
]


@pytest.mark.parametrize("good", ROUND8_COVERAGE)
def test_round8_coverage_selects(good):
    assert safe_eligibility_pick(["I am an Australian citizen", good], VISA) == good


LIVE_RTW_OPTS = [{"text": t, "value": f"v{i}"} for i, t in enumerate(LIVE_RIGHT_TO_WORK)]


async def test_guard_selects_graduate_visa_on_live_dropdown(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(
        label="Which of the following statements best describes your right to work in Australia",
        options=LIVE_RTW_OPTS,
        fid="AU_Q_RTW",
    )
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert len(sel) == 1
    assert sel[0]["text"] == "I have a graduate temporary work visa"
    assert af == []  # rule path never ran
