"""EligibilityAnswerGuard: answer a Seek work-rights dropdown truthfully for the
candidate's actual visa, or hold it for the user. Never claim a status or visa type
they do not hold.

Three false-claim bugs were observed live on the AU right-to-work dropdown, all from
the engine's RULE path (seek_apply._answer_field fuzzy-matches the candidate's visa
label against the options), never the LLM:
  1. the fuzzy matcher picked "I'm an Australian citizen";
  2. it picked "I have a family/partner visa with no restrictions" (matched "no
     restrictions") for a 485 Temporary Graduate holder; and
  3. it HELD "I have a graduate temporary work visa" -- the truthful 485 answer --
     because no fuzzy token matched, stalling the application.

An adversarial audit then showed the fuzzy ladder leaked further false claims: it would
select an option naming a *different* visa subclass/number ("subclass 491 ... full work
rights"), an alternate official visa name ("Skilled Work Regional"), "Australian
national", a negated/conditional no-rights option ("I do not have the right to work ...
without sponsorship"), or an "Other" carrying a sponsorship status -- and a
truth-flipping parenthetical ("graduate temporary work visa (expired)") could collide
into the canonical set. So the guard now answers ONLY via a STRICT exact-match against a
whitelist of truthful phrasings (an option's whole normalised core must equal a canonical
string -- never a loose substring), and HOLDS everything else. A false eligibility claim
must never reach a real submit; holding is always safe.

This wraps seek_apply._answer_field (no vendor edit, like ScreeningInterceptor).
"""
from __future__ import annotations

import importlib
import logging
import re
from contextlib import AbstractContextManager

from ..screening.interceptor import QuestionHeldError, job_id_from_listing

logger = logging.getLogger(__name__)

# Factually false for a non-citizen 485 holder, or a no-/conditional-rights status. Never
# pick an option containing one of these; their presence is also how we recognise an
# eligibility question (is_eligibility_option_set), which is what makes the guard engage
# instead of delegating to the raw rule path.
_DISQUALIFY = (
    "australian citizen",
    "permanent resident",
    "australian pr",
    "new zealand citizen",
    "nz citizen",
    # citizenship-equivalents that carry no "citizen" substring
    "australian national",
    "national of australia",
    "national of this country",
    "nationality",
    "aussie national",
    "australian by birth",
    "born in australia",
    "born here",
    "always lived here",
    "settled here",
    "settled permanently",
    "settled local",
    "permanent local",
    "permanent settler",
    "lawful permanent",
    "no time restrictions",
    "right to remain",
    "new zealander",
    "kiwi",
    # citizenship-by-proxy / permanent-settlement vocabulary (false for a temporary 485)
    "australian passport",
    "naturalis",
    "naturaliz",
    "by descent",
    "dual national",
    "dual citizen",
    "domiciled",
    "ordinarily resident",
    "returning resident",
    "for good",
    "live and work permanently",
    "backpacker",
    "depends on my employer",
    "part of the year",
    "part of the time",
    # standalone citizenship/settlement claims (plain English, no "citizen"/"visa" word).
    # Specific phrases, never bare "australian" (which is a substring of nothing truthful
    # but would over-match "Australian graduate visa").
    "i am australian",
    "i'm australian",
    "yes, i am australian",
    "i am a local",
    "local resident",
    "born and raised",
    "born here",
    "settled status",
    "leave to remain",
    "free to take up employment",
    "free to work here",
    # sponsorship (match the "sponsor" stem anywhere, not a fixed phrase)
    "sponsor",
    # no- / conditional- / negated- work-rights forms
    "not currently entitled",
    "not entitled to work",
    "no right to work",
    "no work rights",
    "no current right to work",
    "no automatic right",
    "do not have the right to work",
    "don't have the right to work",
    "not have the right to work",
    "not eligible to work",
    "ineligible to work",
    "no longer have",
    "only if",
    "must apply",
)

# Specific visa types/categories the candidate does NOT hold (485 Temporary Graduate).
# Picking one is a false claim even if it says "no restrictions" or "full work rights".
# Excluded unless the term is in the candidate's own visa label (so a real
# "graduate"/"temporary" match is never dropped). "graduate" is deliberately absent.
_WRONG_VISA_TYPES = (
    "family",
    "partner",
    "spouse",
    "de facto",
    "parent",
    "student",
    "holiday",
    "working holiday",
    "work and holiday",
    "bridging",
    "business",
    "investor",
    "retirement",
    "refugee",
    "humanitarian",
    "protection",
    "safe haven",
    "skilled regional",
    "skilled work regional",
    "skilled work",
    "regional",
    "temporary activity",
    "activity visa",
    "prospective marriage",
    "marriage",
    "tourist",
    "visitor",
    # a 485 is TEMPORARY; a permanent/indefinite work-rights claim is false
    "permanent",
    "indefinite",
)

# A temporary visa WITH work restrictions is false for a full-work-rights 485. Matched on
# the FULL pre-strip option string (defense-in-depth behind the whitelist paren-strip).
_RESTRICTED = (
    "restrict",  # restriction / restricted / restrictions
    "limited to",
    "limited hour",
    "hrs limited",
    "hours limited",
    "work limitation",
    "part-time",
    "part time",
    "casual only",
    "employer-specific",
    "employer specific",
    "condition 81",  # 8104 / 8105 / 8106 ...
    "8105",
    "fortnight",
)

# Known Australian visa subclass codes. An option naming one of these WITHOUT also naming
# 485 describes a different visa -> reject. (485 + others, e.g. "(e.g. subclass 485, 482)",
# is an example list on an otherwise-generic truthful option, so it is allowed.)
_VISA_SUBCLASS_NUMS = frozenset({
    "100", "186", "187", "188", "189", "190", "300", "309", "408", "417", "457",
    "461", "462", "476", "482", "485", "489", "491", "494", "500", "590", "600",
    "785", "790", "801", "820",
})

_ELIGIBILITY_KEYWORDS = (
    "right to work",
    "work in australia",
    "work rights",
    "working right",
    "citizen",
    "residency",
    "residen",
    "visa",
    "eligible to work",
    "entitled to work",
    "sponsor",
    "work authoris",
    "right to live and work",
    "work status",
    "immigration",
    "nationality",
    "national of",
    "born in",
    "settled",
    "lawful",
    "legally able to",
    "legally entitled",
    "legally permitted",
    "permitted to work",
    "employment authoris",
    "employment authoriz",
    "authorization category",
    "authorisation category",
    "stay and work",
    "work in this country",
    "work in this location",
    "ongoing employment",
)

# First-person lead-ins Seek wraps options in ("I have a graduate temporary work visa").
# Stripped only to canonicalise an option's core for the exact-match below.
_LEADINS = (
    "yes - ", "yes, ", "yes — ", "yes– ",  # affirmatives only; a leading "no" is a negation
    "i have an ", "i have a ", "i hold an ", "i hold a ",
    "i am on an ", "i am on a ", "i'm on an ", "i'm on a ",
    "i am an ", "i am a ", "i'm an ", "i'm a ", "i have ", "i hold ",
    "i am ", "i'm ", "i ",  # bare forms last, so the specific "i am a/an/on" match first
)

# A leading "(I am) not a(n) (Australian) citizen/national [or PR] but [I] [have/am] ..."
# clause: the candidate IS not a citizen, so this prefix is truthful, and the TAIL is what
# is actually claimed. Strip it to match the tail against the canonical set. Safe because
# _ok() screens the FULL original string first, so a false tail (sponsorship / wrong visa)
# is already rejected before the core is ever consulted.
_NEGATED_CITIZEN = re.compile(
    r"^not\s+(an?\s+)?(australian\s+)?(citizen|national)s?\s*[/&,]?\s*(or\s+)?(a\s+)?"
    r"(pr|permanent resident|nz citizen|new zealand citizen)?\s*,?\s*but\s+"
    r"(i\s+)?(am\s+|have\s+|hold\s+)?"
)

# Option cores that are TRUE for the candidate's actual visa (485 Temporary Graduate,
# full work rights, any employer). An option is selected ONLY when its normalised core
# EXACTLY equals one of these -- never a substring -- so "holiday temporary work visa" or
# "subclass 491 visa with full work rights" can never match. The first block names the
# visa explicitly; the second covers the standard generic Seek phrasings (which are
# truthful for a 485) as whole-core exact matches, so a wrong-visa option that merely
# *contains* "allows me to work" cannot leak.
_CANONICAL_485 = frozenset({
    # explicitly names the candidate's actual visa
    "graduate temporary work visa",
    "temporary graduate work visa",
    "graduate work visa",
    "graduate visa",
    "temporary graduate visa",
    "graduate temporary visa",
    "subclass 485",
    "485 visa",
    "post-study work visa",
    "post study work visa",
    "post-study work stream",
    "post study work stream",
    # standard generic full-work-rights phrasings (truthful for a 485)
    "temporary visa that allows me to work in australia",
    "temporary visa that allows me to work",
    "temporary visa with work rights",
    "temporary visa with full work rights",
    "temporary visa with the right to work",
    "temporary visa with no work restrictions",
    "temporary visa with unrestricted work rights",
    "temporary work visa",
    # bare full-work-rights / right-to-work cores (common AU employer phrasings)
    "full work rights",
    "full working rights",
    "full work rights in australia",
    "full working rights in australia",
    "working rights in australia",
    "the right to work in australia",
    "right to work in australia",
    "eligible to work in australia",
    "entitled to work in australia",
    "legally entitled to work in australia",
    "legally able to work in australia",
    "unrestricted work rights",
    "unrestricted work rights in australia",
    "unrestricted working rights in australia",
    "visa with unrestricted work rights",
    # additional common truthful phrasings (additive coverage, all survive _ok)
    "work rights in australia",
    "authorised to work in australia",
    "authorized to work in australia",
    "legally allowed to work in australia",
    "permitted to work in australia",
    "currently have the right to work in australia",
    "visa that allows me to work in australia",
    "valid visa that allows me to work in australia",
    "visa with no work restrictions",
    "temporary work visa with no work restrictions",
    # the candidate's own visa, named explicitly (485 is the only allowed subclass)
    "subclass 485 visa",
    "485 temporary graduate visa",
    "485 graduate visa",
    # further synonym cores (additive coverage, all survive _ok)
    "visa with full work rights",
    "the right to work in australia with no restrictions",
    "unrestricted working rights",
    "visa that allows me to work",
    "visa that permits me to work",
    "temporary resident with work rights",
})

_AFFIRMING = ("485", "graduate", "post-study", "post study")

# A trailing parenthetical is stripped to reach the canonical core ONLY when its ENTIRE
# content (lowercased, all non-alphanumerics removed) EXACTLY equals one of these benign
# 485 clarifiers. Any extra word, digit, negator, or qualifier ("4850", "49 1", "no
# graduate visa", "expired", "casual only", "for Canada") makes the key not-match, so it
# is never stripped. Exact-whitelist > per-word/blacklist: it closes the whole paren
# class (4-digit, split-digit, short-word, flip, restriction, foreign) at once.
_PAREN_CLARIFIERS = frozenset({
    "485", "subclass485", "485visa", "subclass485visa",
    "graduate", "graduatevisa", "temporarygraduate", "graduatetemporary",
    "poststudy", "poststudywork",
})

# Words that FLIP an otherwise-affirming phrase into a false present-tense claim (the visa
# is expired/cancelled/refused/foreign/never-held/...). An affirming parenthetical that
# also carries one of these is NOT stripped, and _ok() rejects the whole option outright.
_FLIP = (
    "expir", "cancel", "ceased", "cease", "lapsed", "revoked", "refused", "rejected",
    "ended", "no longer", "not yet", "awaiting", "pending", "never", "former",
    "previous", "breach", "invalid", "not valid", "withdrawn", "nullified",
    "for canada", "for the uk", "overseas", "abroad", "not granted",
)


def _normalize_core(option: str) -> str:
    """An option's canonical core for exact comparison: lowercased, NBSP folded,
    whitespace collapsed, a leading first-person lead-in removed, and a TRAILING
    parenthetical removed ONLY when it itself affirms a 485/graduate/post-study visa.
    A truth-flipping parenthetical ("(expired)", "(subclass 600 tourist)", "(Vietnam)")
    is kept, so it can never be discarded into a canonical match."""
    s = re.sub(r"\s+", " ", (option or "").replace(" ", " ")).strip().lower()
    m = re.search(r"\s*\(([^)]*)\)\s*$", s)
    if m and re.sub(r"[^a-z0-9]", "", m.group(1)) in _PAREN_CLARIFIERS:
        s = s[: m.start()].strip()
    changed = True
    while changed:
        changed = False
        for lead in _LEADINS:
            if s.startswith(lead):
                s = s[len(lead):]
                changed = True
                break
    s = _NEGATED_CITIZEN.sub("", s, count=1)
    return s.strip(" .,:;-")


def _is_canonical_485(option: str) -> bool:
    return _normalize_core(option) in _CANONICAL_485


def is_eligibility_question(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in _ELIGIBILITY_KEYWORDS)


# Tokens that make an option set "visa/eligibility-shaped" and so MUST engage the guard
# (otherwise a wrong-visa/subclass option worded to dodge _DISQUALIFY would bypass the
# guard and reach the raw vendor path). Curated to be low-collision: multi-word visa
# names and explicit work-rights/status wording, NOT bare type words ("permanent",
# "graduate", "student") that recur in employment-type / seniority / student-status
# dropdowns, and no bare numbers ("$100,000", "4 years"). HOLD stays reachable either way.
_OPTION_MARKERS = (
    # citizenship / settlement status
    "citizen", "permanent resident", "australian pr", "australian national",
    "national of", "nationality", "settled here", "permanent settler", "lawful permanent",
    # work-rights wording
    "right to work", "work rights", "work right", "working right", "work authoris",
    "eligible to work", "entitled to work", "work eligibility", "right to live and work",
    # sponsorship
    "sponsor",
    # visa / subclass / 485 markers
    "visa", "subclass", "485", "graduate temporary", "temporary graduate",
    "post-study", "post study",
    # specific wrong-visa names (multi-word, low collision)
    "skilled work regional", "skilled regional", "working holiday", "work and holiday",
    "prospective marriage", "temporary protection", "safe haven", "temporary activity",
    # permanence / authorisation / duration cues (adverb/multi-word forms avoid the bare
    # "Permanent" employment-type and "Graduate" seniority collisions)
    "permanently", "indefinitely", "indefinite", "without limitation", "no time limit",
    "no end date", "authoris", "authoriz", "ongoing employment", "stay and work",
    # informal visa / conditional-rights synonyms
    "backpacker", "bridging", "regional scheme", "spouse arrangement",
    "depends on my employer", "part of the year", "part of the time",
)


def is_eligibility_option_set(option_texts) -> bool:
    """True when the option set looks visa/eligibility-shaped: it offers a disqualifying
    status (citizen / PR / national / no-rights / sponsorship), names a visa type or
    subclass, or carries explicit work-rights wording. Engages on _OPTION_MARKERS OR any
    _DISQUALIFY term, so detection is never narrower than rejection -- the guard can never
    DELEGATE an option that _ok() would reject. Plain experience / salary / notice
    dropdowns carry none of these and delegate untouched."""
    for t in (option_texts or []):
        lo = str(t).lower()
        if any(m in lo for m in _OPTION_MARKERS) or any(d in lo for d in _DISQUALIFY):
            return True
    return False


def _ok(option: str, hint: str) -> bool:
    """Defense-in-depth filter: reject any option that asserts a false status, a wrong
    visa type, work restrictions, or a non-485 visa subclass number."""
    lo = option.lower()
    if any(d in lo for d in _DISQUALIFY):
        return False
    if any(f in lo for f in _FLIP):
        return False
    if any(w in lo and w not in hint for w in _WRONG_VISA_TYPES):
        return False
    # restriction wording, but not the POSITIVE forms ("unrestricted" / "no restrictions"
    # / "without restriction"), which are truthful full-work-rights statements for a 485
    restr = lo
    for pos in ("unrestricted", "no restriction", "no work restriction",
                "without restriction", "with no restriction"):
        restr = restr.replace(pos, "")
    if any(r in restr for r in _RESTRICTED):
        return False
    # reject if ANY non-485 visa subclass number is present, even when 485 co-occurs
    # ("(subclass 485 or 491)") or is split by spaces ("49 1"). Scan the space-flattened
    # string so "49 1" -> "491" is caught.
    flat = re.sub(r"\s+", "", lo)
    if set(re.findall(r"\d{3}", flat)) & _VISA_SUBCLASS_NUMS - {"485"}:
        return False
    return True


def safe_eligibility_pick(options, hint: str = "") -> str | None:
    """The truthful option for the candidate's actual visa (485 Temporary Graduate, full
    work rights, no sponsorship), or None to HOLD. Selects ONLY via a strict exact-match:
    an option survives the _ok() filter AND its whole normalised core equals a canonical
    truthful phrasing. Anything else holds -- there is no fuzzy/generic/"Other" fallback,
    because the adversarial audit proved those paths leak false claims."""
    h = (hint or "").lower()
    eligible = [str(o) for o in (options or []) if _ok(str(o), h)]
    for o in eligible:
        if _is_canonical_485(o):
            return o
    return None


class EligibilityAnswerGuard(AbstractContextManager):
    def __init__(self, module_name: str = "seek_apply"):
        self._module_name = module_name
        self._mod = None
        self._original = None
        self._original_select = None
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
        except Exception:
            return
        if not hasattr(mod, "_answer_field"):
            return
        original = mod._answer_field  # type: ignore[attr-defined]

        async def _patched(page, q, job, candidate):
            tag = q.get("tag")
            options = q.get("options") or []
            label = q.get("label") or q.get("placeholder") or q.get("sectionText") or ""
            if tag == "SELECT" and options:
                texts = [str(o.get("text", "")) for o in options]
                if is_eligibility_question(label) or is_eligibility_option_set(texts):
                    hint = str(getattr(mod, "CAND_VISA_LABEL", "") or "")
                    pick = safe_eligibility_pick(texts, hint)
                    if pick is not None:
                        chosen = next(
                            (o for o in options if str(o.get("text", "")) == pick), None
                        )
                        logger.info(
                            "Eligibility guard: answering %r with %r (visa=%r)",
                            str(label)[:80],
                            pick,
                            hint,
                        )
                        await mod._safe_select_option(  # type: ignore[attr-defined]
                            page,
                            q.get("id"),
                            desired_value=(chosen or {}).get("value"),
                            desired_text=(chosen or {}).get("text"),
                        )
                        return
                    logger.warning(
                        "Eligibility guard: no truthful option for %r among %r; "
                        "HOLDING for the user (visa=%r)",
                        str(label)[:80],
                        texts,
                        hint,
                    )
                    raise QuestionHeldError(job_id_from_listing(job), str(label) or "work rights")
            return await original(page, q, job, candidate)

        # Chokepoint: refuse to SELECT any option whose text asserts a disqualifying
        # status, no matter which path chose it. This backstops a detection bypass -- even
        # if a status dropdown evades both gates and the raw vendor path tries to select a
        # false citizenship/PR/visa claim, the selection is vetoed and the question holds.
        original_select = getattr(mod, "_safe_select_option", None)
        if original_select is not None and callable(original_select):
            async def _patched_select(page, field_id, desired_value=None, desired_text=None):
                lo = str(desired_text or "").lower()
                if lo and any(d in lo for d in _DISQUALIFY):
                    logger.warning(
                        "Eligibility chokepoint: refusing false-status option %r; HOLDING",
                        str(desired_text)[:80],
                    )
                    raise QuestionHeldError("", str(desired_text)[:80] or "work rights")
                return await original_select(
                    page, field_id, desired_value=desired_value, desired_text=desired_text
                )

            self._original_select = original_select
            mod._safe_select_option = _patched_select  # type: ignore[attr-defined]

        self._mod = mod
        self._original = original
        mod._answer_field = _patched  # type: ignore[attr-defined]
        self._installed = True
        logger.info("EligibilityAnswerGuard installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            if self._mod is not None:
                self._mod._answer_field = self._original  # type: ignore[attr-defined]
                if self._original_select is not None:
                    self._mod._safe_select_option = self._original_select  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False

    def __enter__(self) -> "EligibilityAnswerGuard":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
