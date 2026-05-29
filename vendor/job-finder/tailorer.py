import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from claude_cli import DEFAULT_MODEL, claude_complete
from models import JobListing
from utils import (
    detect_libreoffice,
    extract_json,
    load_profile,
    make_filename,
    retry_async,
)

logger = logging.getLogger(__name__)
_libreoffice_path: str | None = None
_pdf_lock = asyncio.Lock()

ASSETS_DIR = Path("assets")
OUTPUT_DIR = Path("output")

# Phrases that indicate refusal — letter argues it cannot be written.
_REFUSAL_MARKERS = (
    "i cannot write",
    "i can't write",
    "i am unable to write",
    "i'm unable to write",
    "please provide the full job description",
    "please provide the job description",
    "without the actual job description",
    "without the job description",
    "without the jd",
    "company-facts constraint",
    "truthfulness constraint",
    "would violate",
    "violate the truthfulness",
    "violate the company-facts",
)

# Phrases where the letter argues the candidate is NOT a fit — withdrawal, not application.
_SELF_DISQUALIFY_MARKERS = (
    "i have no production experience",
    "i have no experience with",
    "i have no track record",
    "no hands-on experience",
    "won't write a cover letter",
    "will not write a cover letter",
    "this role is not a good fit",
    "is not a good fit for my",
    "this isn't a good fit",
    "would be dishonest",
    "best of luck filling",
    "set us up for failure",
    "set both of us up for failure",
    "your hiring team needs someone",
    "you need someone with direct",
    "with a candidate whose experience genuinely matches",
    "withdrawing my application",
    "discontinuing my application",
    "decline this opportunity",
    "i am not the candidate",
    "i'm not the candidate",
    "that's not me",
    "i won't misrepresent",
    "claiming adjacency",
)


# Phrases where the model narrates its own reasoning into the letter body.
_META_LEAK_MARKERS = (
    "i will write based on",
    "i will treat this",
    "i will treat the role",
    "i'll write based on",
    "i'll treat this",
    "the candidate's profile",
    "the candidate has",
    "candidate profile",
    "the profile says",
    "the profile contains",
    "internal notes",
    "(a) strong match",
    "(b) adjacent",
    "(c) gap",
    "as a strong match given",
    "treat this as a strong match",
    "the job description is not available, so",
    "the job description is unavailable, so",
    "the job description contains no",
    "grounded entirely in the candidate profile",
    "grounded only in the candidate profile",
    "grounded only in what the profile",
    "given the candidate's direct",
    # Prompt-section header echoes — model wrote internal-only steps as letter body.
    "adjacency detection",
    "jd core technologies",
    "profile core technologies",
    "core technologies the role demands",
    "step 1 (internal audit",
    "internal audit",
)


# Letters longer than this are dumping prompt instructions / chain-of-thought
# rather than writing a real cover letter (target is under 350 words ≈ 2200 chars).
_MAX_LETTER_CHARS = 2800


class CoverLetterQualityError(Exception):
    """Raised when a generated cover letter fails the quality gate.
    Caller should mark the application as skipped with the structured reason."""

    def __init__(self, reason: str, *, jd_len: int, output_preview: str = ""):
        self.reason = reason
        self.jd_len = jd_len
        self.output_preview = output_preview
        super().__init__(f"{reason} (jd_len={jd_len})")


_MARKER_KINDS = (
    ("refusal_marker", _REFUSAL_MARKERS),
    ("self_disqualify_marker", _SELF_DISQUALIFY_MARKERS),
    ("meta_leak_marker", _META_LEAK_MARKERS),
)


def _quality_gate(text: str) -> str | None:
    """Return None if the letter is shippable, else a short reason string
    (e.g. "refusal_marker:'i cannot write'", "missing_signoff", "too_short:N").
    Marker checks run before length so a short refusal reads as a refusal."""
    if not text:
        return "empty_output"
    body = text.strip()
    lower = body.lower()
    for kind, markers in _MARKER_KINDS:
        for marker in markers:
            if marker in lower:
                return f"{kind}:{marker!r}"
    if "sagar verma" not in lower:
        return "missing_signoff"
    if len(body) < 400:
        return f"too_short:{len(body)}"
    if len(body) > _MAX_LETTER_CHARS:
        return f"too_long:{len(body)}"
    return None


def _looks_like_refusal(text: str) -> bool:
    """Backward-compatible wrapper around _quality_gate."""
    return _quality_gate(text) is not None


# Section headings that signal "this is the part of the JD that lists the
# actual requirements / day-to-day work". When present, we prefer these
# sections over the boilerplate (about-us, benefits, EEO statement) that
# typically sits at the top of an Australian Seek post.
_JD_RELEVANT_HEADINGS = (
    "what you'll do", "what you will do",
    "what you'll bring", "what you will bring",
    "what we're looking for", "what we are looking for",
    "what you'll need", "what you will need",
    "about the role", "about you", "about the position",
    "the role", "the opportunity",
    "responsibilities", "key responsibilities",
    "requirements", "key requirements", "minimum requirements",
    "skills", "skills and experience", "qualifications",
    "your experience", "your background",
    "required skills", "essential skills", "essential criteria",
    "must have", "must-have", "nice to have", "nice-to-have",
    "duties",
)


def extract_jd_relevant(text: str, max_chars: int = 4000) -> str:
    """Pull the requirements/responsibilities sections of a JD ahead of
    boilerplate when the post is too long for the budget. If the post is
    short, return as-is. If we can't find any known headings, fall back
    to a sentence-boundary truncation.

    Pure function; safe to unit-test."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    # Index every line that looks like a heading we care about.
    relevant_starts: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip().lower().rstrip(":")
        # Keep short lines only; long sentences mentioning these words aren't headings.
        if len(stripped) <= 60 and stripped in _JD_RELEVANT_HEADINGS:
            relevant_starts.append(i)

    if relevant_starts:
        # Pull each relevant section (heading line plus subsequent lines until
        # the next heading or a blank line followed by another short title).
        chunks: list[str] = []
        for start in relevant_starts:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                cand = lines[j].strip().lower().rstrip(":")
                if cand in _JD_RELEVANT_HEADINGS:
                    end = j
                    break
            chunks.append("\n".join(lines[start:end]).strip())
        joined = "\n\n".join(chunks)
        if len(joined) <= max_chars:
            return joined
        # Even concatenated relevant sections are too long; take a clean prefix.
        return _truncate_at_sentence(joined, max_chars)

    # No headings recognised; fall back to sentence-boundary truncation.
    return _truncate_at_sentence(text, max_chars)


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Cut at the last sentence boundary inside max_chars (avoids cutting
    a requirement mid-word)."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Find the last terminal punctuation followed by whitespace or newline.
    m = re.search(r"[.!?](?=\s|$)(?!.*[.!?](?=\s|$))", cut, flags=re.DOTALL)
    if m:
        return cut[: m.end()].strip()
    # No sentence boundary found; fall back to the last newline.
    nl = cut.rfind("\n")
    return cut[:nl].strip() if nl > max_chars // 2 else cut


async def tailor(job: JobListing, tier: str = "full") -> tuple[str, str]:
    """Returns (resume_pdf_path, cover_letter_pdf_path).

    Raises CoverLetterQualityError if the cover letter fails the quality gate.

    tier="base" uses the base resume docx as-is (no LLM resume tailoring) but
    still generates an LLM cover letter; "full"/"quick" both LLM-tailor both
    documents.
    """
    global _libreoffice_path
    if _libreoffice_path is None:
        _libreoffice_path = detect_libreoffice()

    if tier == "base":
        cover_text, resume_pdf = await asyncio.gather(
            _generate_cover_letter(job),
            _export_base_resume_pdf(job),
        )
        cover_pdf = _export_cover_letter_pdf(cover_text, job)
        return resume_pdf, cover_pdf

    cover_text, sections = await asyncio.gather(
        _generate_cover_letter(job),
        _tailor_resume_text(job),
    )
    resume_pdf = await _export_resume_pdf(sections, job)
    cover_pdf = _export_cover_letter_pdf(cover_text, job)
    return resume_pdf, cover_pdf


async def _export_base_resume_pdf(job: JobListing) -> str:
    """Convert the base SAGAR VERMA.docx to PDF as-is, with the per-job filename."""
    filename = make_filename("SagarVerma", job.company, job.title)
    base_docx = ASSETS_DIR / "SAGAR VERMA.docx"
    temp_docx = OUTPUT_DIR / filename.replace(".pdf", ".docx")
    # Copy base docx into the per-job filename, then convert
    import shutil
    shutil.copy(base_docx, temp_docx)
    try:
        async with _pdf_lock:
            await _run_libreoffice(str(temp_docx), str(OUTPUT_DIR))
    finally:
        temp_docx.unlink(missing_ok=True)
    pdf_path = OUTPUT_DIR / filename
    if not pdf_path.exists():
        raise RuntimeError(f"LibreOffice did not produce {pdf_path}")
    return str(pdf_path)


_RESUME_SYSTEM_RULES = (
    "You are a truthful resume tailor. Your ONLY source of facts is the candidate "
    "profile that follows. You must not invent experience, tools, technologies, job "
    "titles, products, frameworks, platforms, methodologies, or metrics that are not "
    "present in the profile. Your job is to reorder, re-emphasise and rephrase existing "
    "profile content so that the most JD-relevant parts come first and are described "
    "using terminology the ATS will recognise. Keyword integration happens through the "
    "Skills line and through word choice when rephrasing genuine experience, never by "
    "fabricating new experience.\n\n"
    "STEP 1 (INTERNAL AUDIT, perform silently before writing JSON):\n"
    "  a. Identify the top 3-5 demands in the Job Description (what the candidate must do "
    "     on day one to be valuable).\n"
    "  b. For each demand, locate the SINGLE strongest supporting fact in the profile "
    "     (a project, tool, metric, or experience).\n"
    "  c. Plan bullet 1 around demand 1, bullet 2 around demand 2, and so on. "
    "     Demands the profile cannot support get NO bullet. Do not fabricate.\n"
    "  d. Then write the JSON.\n\n"
    "INTERNAL-NOTES MARKER: The profile contains a section after the line starting with "
    "'===== INTERNAL NOTES'. EVERYTHING below that marker is context for YOU only. You must "
    "NEVER surface any fact, skill, tool, or experience from below that marker in the "
    "generated summary, bullets, skills line, or experience headline. Treat it as invisible "
    "for output purposes.\n\n"
    "HARD CONSTRAINTS (these override everything else):\n"
    "1. Every bullet, every sentence in the summary, and the experience headline must "
    "   be grounded in the profile. If a tool, product, platform, methodology, or metric "
    "   is not named in the profile, do NOT put it in a bullet or the summary or the "
    "   experience headline. No exceptions.\n"
    "2. You may rephrase profile content using close synonyms (for example 'CI/CD pipelines' "
    "   can be described as 'automated deployment pipelines'). You may NOT swap the underlying "
    "   technology (for example CI/CD is not Power Automate, AWS IAM is not Azure AD, "
    "   CloudFormation is not Dataverse).\n"
    "3. If the JD asks for experience the profile does not contain (for example Power Apps, "
    "   Salesforce, ServiceNow, Workday, SAP, Databricks, Snowflake, Oracle DBA, etc.), DO NOT "
    "   invent it. Use the closest genuine transferable experience and let the Skills line "
    "   and summary reflect interest/adjacency without claiming hands-on delivery.\n"
    "4. The experience_headline must be a title the candidate can defend in interview. It may "
    "   echo JD wording only when the profile supports it. When in doubt, keep the real title "
    "   from the profile.\n\n"
    "SUMMARY RULES:\n"
    "- Under 100 words. Grounded in the profile. Lead with years of experience and the\n"
    "  real primary specialisation (cloud/DevOps/AWS), then note adjacency to the JD's\n"
    "  focus where genuine. Do not claim seniority in a tool the profile does not list.\n"
    "- Be specific: mention real years, scale, and tools drawn from the profile.\n"
    "- No AI filler words (pivotal, leveraging, vibrant, testament, foster, enhance, "
    "  crucial, passionate, spearheaded).\n\n"
    "BULLET RULES:\n"
    "- Exactly 8 bullets. Each bullet must correspond to something the profile actually says.\n"
    "- ORDER MATTERS MORE THAN WORDING. Bullet 1 must address the JD's #1 demand from the\n"
    "  Step 1 audit. Bullet 2 the #2 demand. The first 3 bullets get scanned in 6 seconds;\n"
    "  bullets 6-8 may not be read at all. Put the strongest JD-aligned proof at the top.\n"
    "- Format: [past-tense action verb] + [specific task grounded in profile] + [result\n"
    "  quantified using numbers/percentages/dollar amounts that appear in the profile].\n"
    "- Do NOT invent metrics. If no metric exists for a given item, write the bullet\n"
    "  without a fabricated number.\n"
    "- Do NOT substitute the candidate's real tooling for JD tooling. Example: if the\n"
    "  profile says 'CI/CD pipelines reduced deployment effort by 70%', do NOT rewrite\n"
    "  this as 'Power Automate reduced manual effort by 70%'.\n"
    "- Vary sentence structure. No em dashes.\n"
    "- EXAMPLES:\n"
    "  GOOD: 'Cut AWS spend 28% by right-sizing 40+ EC2 instances and migrating idle\n"
    "         workloads to Spot, freeing $14k/month for reinvestment.'\n"
    "         (real verb, real tools, real metric, real outcome, all from profile)\n"
    "  BAD:  'Leveraged synergistic cloud strategies to facilitate cost optimisation\n"
    "         across the AWS ecosystem.'\n"
    "         (banned words, vague, no metric, no proof. REJECT this style.)\n\n"
    "SKILLS RULES (this is the only place aggressive keyword integration is allowed):\n"
    "- Single compact comma-separated string, max 220 characters.\n"
    "- You may include JD keywords the candidate has GENUINE exposure to (listed in the\n"
    "  profile's CORE SKILLS or Applied AI and LLM Integration sections, or reasonable\n"
    "  transferable concepts like 'REST APIs', 'IAM', 'stakeholder management').\n"
    "- Do NOT include JD-specific product names the profile does not mention (for\n"
    "  example do not list Power Apps, Dataverse, Salesforce, SAP unless the profile\n"
    "  explicitly lists them).\n\n"
    "EXPERIENCE HEADLINE RULES:\n"
    "- Max 6 words. Must be defensible against the profile. If the JD title is outside\n"
    "  the profile's genuine scope, prefer the profile's real title (for example\n"
    "  'DevOps / Cloud Engineer') optionally with a relevant adjacent qualifier.\n\n"
    "IMPORTANT: Always return valid JSON regardless of whether a job description was "
    "provided. If no JD is available, tailor based on the job title alone within the "
    "hard constraints above.\n"
    "Return JSON only, no markdown fences:\n"
    '{"summary": "...", "bullets": ["...", ...], '
    '"skills": "...", "experience_headline": "..."}'
)


async def _tailor_resume_text(job: JobListing) -> dict:
    """Generate tailored resume sections JSON via the `claude` CLI."""
    profile = load_profile()
    jd_text = (job.description or "").strip()
    jd_block = extract_jd_relevant(jd_text, 4000) if jd_text else "(none)"
    job_block = (
        f"JOB TITLE: {job.title}\nCOMPANY: {job.company}\n\n"
        f"Job Description:\n{jd_block}\n\n"
        "Now produce the JSON per the rules above."
    )

    async def _call():
        text = await claude_complete(
            system=(
                _RESUME_SYSTEM_RULES
                + "\n\nCandidate Profile (THE ONLY SOURCE OF TRUTH):\n"
                + profile
            ),
            user=job_block,
            model=DEFAULT_MODEL,
        )
        try:
            return extract_json(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Unexpected resume CLI response: {text!r}") from exc

    return await retry_async(_call)


_COVER_LETTER_SYSTEM_RULES = (
    "Write a strong, specific, and TRUTHFUL cover letter for the job application "
    "described in the user message. Your ONLY source of facts about the candidate "
    "is the candidate profile that follows.\n\n"
    "TRUTHFULNESS CONSTRAINTS:\n"
    "- Do not claim experience with any tool, product, platform or methodology that is\n"
    "  not in the profile. If the JD emphasises something the candidate does not have,\n"
    "  acknowledge it honestly as adjacent or transferable, do not invent.\n"
    "- Every concrete claim must be traceable to the profile text.\n"
    "- Numbers, timelines, and scale must come from the profile, never fabricated.\n"
    "- The profile includes an '===== INTERNAL NOTES' marker. EVERYTHING below that\n"
    "  marker is context for YOU only and must never appear in the cover letter.\n\n"
    "COMPANY-FACTS CONSTRAINT (HARD RULE; instant credibility killer if violated):\n"
    "- Do NOT make any factual claim about the company itself (its products,\n"
    "  leadership, history, mission, market position, recent news, awards, scale,\n"
    "  industry reputation, or culture) UNLESS that exact claim appears in the\n"
    "  Job Description text shown above.\n"
    "- If the JD does not mention something, you do not know it. Do not infer from\n"
    "  the company name. Do not draw on training-data knowledge of the company.\n"
    "- BAD: 'Your innovative approach to cloud transformation aligns with...'\n"
    "       (unless 'innovative approach to cloud transformation' is in the JD)\n"
    "- BAD: 'As an industry leader in financial services, your team...'\n"
    "       (you do not know this; do not say it)\n"
    "- GOOD: Reference what the JD ITSELF says. 'The role calls out consolidating\n"
    "        three AWS accounts under one billing org. I led that exact migration\n"
    "        at [profile-grounded employer].'\n"
    "- If you have nothing JD-grounded to say about the company, skip the company\n"
    "  reference entirely. Talking about the role is enough.\n\n"
    "ADJACENCY DETECTION (do this before writing):\n"
    "- Read the JD and list the core technologies/platforms it demands.\n"
    "- Look up each in the profile sections: CORE SKILLS, TECHNICAL SKILLS, "
    "  PROFESSIONAL EXPERIENCE, ADDITIONAL STRENGTHS. Anything appearing there "
    "  reflects what the candidate has actually built or operated.\n"
    "- Classify the role as one of:\n"
    "    (A) STRONG MATCH: the JD's core tech appears in the profile sections above.\n"
    "        Use the candidate's strongest specific evidence (a shipped product, a\n"
    "        production system, a real metric) for the body of the letter.\n"
    "    (B) ADJACENT: JD core is a different vendor/product but the same underlying\n"
    "        concept exists in the profile (Azure/AWS, GCP/AWS, Power Automate/CI-CD,\n"
    "        Azure AD/AWS IAM, OpenAI or Bedrock/Anthropic Claude API, Snowflake/RDS+S3).\n"
    "    (C) GAP: JD core is outside profile scope entirely (for example Power Apps,\n"
    "        Dataverse, Salesforce, SAP, ServiceNow, Workday, Oracle DBA, Databricks,\n"
    "        .NET, Java Spring, Ruby on Rails). Reframe around the candidate's real\n"
    "        strengths; do not over-promise the GAP tech.\n\n"
    "GAP / ADJACENT PARAGRAPH EXAMPLES (for paragraph 3):\n"
    "  GOOD (JD wants Azure, profile is AWS):\n"
    "    'My deepest production work is in AWS rather than Azure. The IAM design,\n"
    "    cost-discipline, and IaC patterns translate cleanly because I have run\n"
    "    multi-account environments with Terraform and CI/CD enforcement, which is\n"
    "    exactly what your role calls out.'\n"
    "    (Two sentences. Honest. Concrete proof. Stops.)\n"
    "  BAD:\n"
    "    'While I have not directly worked with Azure, I am eager to learn and adapt.\n"
    "    I am confident my passion for cloud will allow me to ramp up quickly. I would\n"
    "    love to bring my enthusiasm to your team.'\n"
    "    (Banned words, no proof, junior tone. REJECT.)\n\n"
    "STRUCTURE: 4 paragraphs.\n"
    "1. Opening: hook based on a specific requirement from the JD or something "
    "   notable about the company. No generic openers. No 'I am writing to'.\n"
    "2. Most relevant experience: pick the 1-2 experiences from the profile that "
    "   best match the JD. Be specific with numbers and outcomes that the profile "
    "   actually states.\n"
    "3. Adjacency paragraph (REPLACES 'Why this role/company' for ADJACENT or GAP "
    "   roles; for STRONG MATCH roles, keep it as 'Why this role/company'):\n"
    "   - For STRONG MATCH: connect the candidate's background directly to the JD.\n"
    "   - For ADJACENT: state the core strength honestly (AWS, DevOps, applied AI "
    "     on Anthropic Claude), then frame the JD's stack as a close transfer. One "
    "     sentence on why the concepts carry across. Do not claim hands-on delivery "
    "     on the adjacent stack if the profile does not back it.\n"
    "   - For GAP: lead with the candidate's genuine strengths, then a single "
    "     honest sentence acknowledging that [JD core tech] is not the primary "
    "     focus of the profile, framed as a deliberate adjacency, not a gap to "
    "     hide. Suggest the transferable skills that apply (for example integration "
    "     design, IAM/security thinking, enterprise delivery rigour, applied AI "
    "     automation experience). Keep it confident and brief. One sentence of "
    "     honest acknowledgement plus one sentence of genuine transferable value.\n"
    "   - Never use the words 'unfortunately', 'lack', 'weakness', 'limited' in "
    "     this paragraph. Frame as 'primary focus is X, with Y as adjacent'.\n"
    "4. Short close: confident, direct. No 'I look forward to hearing from you'.\n\n"
    "WRITING RULES (strictly follow):\n"
    "- Do NOT open with 'I am writing to...', 'I am excited/thrilled/delighted', 'I am passionate about'.\n"
    "- Do NOT use: pivotal, leveraging, vibrant, testament, delve, foster, enhance, showcase, "
    "highlight, underscore, spearheaded, groundbreaking, renowned, crucial, additionally, align with.\n"
    "- Do NOT use em dashes.\n"
    "- Do NOT use rule-of-three structures.\n"
    "- Do NOT end with 'I look forward to hearing from you' or similar generic closers.\n"
    "- Use simple words: 'use' not 'leverage', 'built' not 'developed and implemented'.\n"
    "- Cite actual numbers, tools, or outcomes from the candidate profile.\n"
    "- Vary sentence length. Some short. Some longer.\n"
    "- Write like a confident professional, not a desperate applicant.\n"
    "- Keep it under 350 words total.\n\n"
    "Format: Start directly with the opening paragraph. No header, no 'Dear Hiring Manager', no 'Re:' line. "
    "End with just 'Sagar Verma' as the sign-off, followed by a new line with: "
    "sagarvd130@gmail.com | linkedin.com/in/sagarvbuilds | https://sagarverma.cv\n"
    "Return ONLY the cover letter text, no markdown."
)


async def _generate_cover_letter(job: JobListing) -> str:
    """Generate a cover letter via the `claude` CLI. Raises
    CoverLetterQualityError if the output fails the quality gate; caller
    skips the application with the structured reason."""
    profile = load_profile()
    jd_text = (job.description or "").strip()
    jd_block = extract_jd_relevant(jd_text, 4000) if jd_text else "(none)"
    job_block = (
        f"JOB TITLE: {job.title}\nCOMPANY: {job.company}\n\n"
        f"JOB DESCRIPTION:\n{jd_block}\n\n"
        "Now write the cover letter per the rules above."
    )

    async def _call():
        return await claude_complete(
            system=(
                _COVER_LETTER_SYSTEM_RULES
                + "\n\nCANDIDATE PROFILE (THE ONLY SOURCE OF TRUTH):\n"
                + profile
            ),
            user=job_block,
            model=DEFAULT_MODEL,
        )

    text = await retry_async(_call)

    reason = _quality_gate(text)
    if reason is not None:
        logger.error(
            "Cover letter quality gate FAILED: %s | %s @ %s | jd_len=%d | preview=%r",
            reason, job.title, job.company, len(jd_text), text[:200],
        )
        raise CoverLetterQualityError(reason, jd_len=len(jd_text), output_preview=text[:500])

    return text


def _add_hyperlink(paragraph, url: str, text: str):
    """Add a clickable hyperlink to a docx paragraph."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    import docx.opc.constants

    part = paragraph.part
    r_id = part.relate_to(url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")

    # Blue color and underline to match typical hyperlink style
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)

    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)

    # Match the font size of existing runs
    if paragraph.runs:
        existing_rpr = paragraph.runs[0]._element.find(qn("w:rPr"))
        if existing_rpr is not None:
            sz = existing_rpr.find(qn("w:sz"))
            if sz is not None:
                new_sz = OxmlElement("w:sz")
                new_sz.set(qn("w:val"), sz.get(qn("w:val")))
                rPr.append(new_sz)
            szCs = existing_rpr.find(qn("w:szCs"))
            if szCs is not None:
                new_szCs = OxmlElement("w:szCs")
                new_szCs.set(qn("w:val"), szCs.get(qn("w:val")))
                rPr.append(new_szCs)
            rFonts = existing_rpr.find(qn("w:rFonts"))
            if rFonts is not None:
                new_rFonts = OxmlElement("w:rFonts")
                for attr in rFonts.attrib:
                    new_rFonts.set(attr, rFonts.get(attr))
                rPr.append(new_rFonts)

    new_run.append(rPr)
    new_run.text = text
    hyperlink.append(new_run)
    paragraph._element.append(hyperlink)


async def _export_resume_pdf(sections: dict, job: JobListing) -> str:
    filename = make_filename("SagarVerma", job.company, job.title)
    doc = Document(ASSETS_DIR / "SAGAR VERMA.docx")

    summary = sections.get("summary", "")
    bullets = sections.get("bullets", [])
    skills_text = sections.get("skills", "")
    headline = sections.get("experience_headline", "")

    # --- Add LinkedIn and website to contact line if not already present ---
    LINKEDIN_URL = "https://www.linkedin.com/in/sagarvbuilds/"
    LINKEDIN_TEXT = "linkedin.com/in/sagarvbuilds"
    WEBSITE_URL = "https://sagarverma.cv"
    WEBSITE_TEXT = "https://sagarverma.cv"

    contact_para = None
    for para in doc.paragraphs:
        if "@" in para.text:
            contact_para = para
            break

    if contact_para and LINKEDIN_TEXT not in contact_para.text:
        # Add " | " separator
        if contact_para.runs:
            contact_para.runs[-1].text += " | "
        else:
            contact_para.add_run(" | ")
        # Add LinkedIn as clickable hyperlink
        _add_hyperlink(contact_para, LINKEDIN_URL, LINKEDIN_TEXT)
        # Add " | " separator
        contact_para.add_run(" | ")
        # Add website as clickable hyperlink
        _add_hyperlink(contact_para, WEBSITE_URL, WEBSITE_TEXT)

    if contact_para and summary:
        from docx.oxml import OxmlElement
        new_p = OxmlElement("w:p")
        contact_para._element.addnext(new_p)
        # Locate the newly inserted paragraph via XML element identity (doc.paragraphs
        # creates new wrapper objects, so we match by element reference)
        summary_para = next(
            p for p in doc.paragraphs if p._element is new_p
        )
        summary_para.style = doc.styles["Normal"]
        summary_para.add_run(summary)

    # --- Patch remaining sections ---
    in_work_experience = False
    bullets_applied = 0

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        # Track current section via Heading 1 (top-level sections only; skip company sub-headings)
        if para.style.name == "Heading 1":
            section_lower = text.lower()
            # Only update the flag for recognised top-level section headings
            if any(kw in section_lower for kw in ("work experience", "experience", "education",
                                                    "skills", "certifications", "summary")):
                in_work_experience = ("work experience" in section_lower
                                      or section_lower == "experience")
            # Company / role sub-headings inside the section — leave flag unchanged
            continue

        # Bold Normal paragraphs: either name (skip) or skills line (replace)
        if para.style.name == "Normal" and any(r.bold for r in para.runs) and "@" not in text:
            if text.startswith("Skills:") and skills_text:
                for run in para.runs:
                    run.text = ""
                if para.runs:
                    para.runs[0].text = f"Skills: {skills_text}"
            continue

        # Experience headline: Normal style, tab separator, contains a year digit sequence
        if (para.style.name == "Normal" and "\t" in text and in_work_experience
                and headline
                and any(part.strip().isdigit() for part in text.replace("-", " ").split())):
            date_part = text.split("\t", 1)[1] if "\t" in text else ""
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = f"{headline}\t{date_part}"
            continue

        # Work experience bullets
        if (para.style.name == "List Paragraph" and in_work_experience
                and bullets_applied < len(bullets)):
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = bullets[bullets_applied]
            bullets_applied += 1
            continue

    temp_docx = OUTPUT_DIR / filename.replace(".pdf", ".docx")
    doc.save(temp_docx)

    try:
        async with _pdf_lock:
            await _run_libreoffice(str(temp_docx), str(OUTPUT_DIR))
    finally:
        temp_docx.unlink(missing_ok=True)

    pdf_path = OUTPUT_DIR / filename
    if not pdf_path.exists():
        raise RuntimeError(f"LibreOffice did not produce {pdf_path}")
    return str(pdf_path)


async def _run_libreoffice(docx_path: str, out_dir: str):
    result = await asyncio.to_thread(
        subprocess.run,
        [_libreoffice_path, "--headless", "--convert-to", "pdf",
         "--outdir", out_dir, docx_path],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice error: {result.stderr.decode()}")


def _export_cover_letter_pdf(text: str, job: JobListing) -> str:
    filename = make_filename("CoverLetter", job.company, job.title)
    path = OUTPUT_DIR / filename
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    margin = 72
    max_width = width - 2 * margin
    c.setFont("Helvetica", 11)
    y = height - margin

    for paragraph in text.split("\n"):
        if not paragraph.strip():
            y -= 8  # blank line gap
            continue

        # Word-wrap each paragraph to fit within margins
        words = paragraph.split()
        line = ""
        for word in words:
            test = f"{line} {word}".strip()
            if c.stringWidth(test, "Helvetica", 11) <= max_width:
                line = test
            else:
                if y < margin + 20:
                    c.showPage()
                    y = height - margin
                c.drawString(margin, y, line)
                y -= 16
                line = word
        if line:
            if y < margin + 20:
                c.showPage()
                y = height - margin
            c.drawString(margin, y, line)
            y -= 16

        y -= 4  # paragraph spacing

    c.save()
    return str(path)
