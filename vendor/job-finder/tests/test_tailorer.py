# tests/test_tailorer.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path
from models import JobListing

JOB = JobListing(
    url="https://au.seek.com/job/1",
    title="Platform & Automation Architect",
    company="Interactive Pty Ltd",
    board="seek",
    description="Terraform, AWS, Kubernetes, CI/CD pipelines required.",
    easy_apply=True,
)

EXPANDED_RESPONSE = (
    '{"summary": "Platform engineer with 4+ years AWS and Terraform expertise.", '
    '"bullets": ["Deployed Terraform IaC across 15+ AWS accounts reducing drift by 40%.", '
    '"Built CI/CD pipelines with GitHub Actions cutting deploy time by 70%.", '
    '"Led Kubernetes migration on AWS ECS improving scalability by 30%.", '
    '"Reduced AWS costs by US$4k/month through rightsizing and S3 lifecycle.", '
    '"Maintained 99.9% uptime across 200+ Linux servers in production.", '
    '"Implemented CloudWatch and Datadog alerting reducing MTTR by 20%.", '
    '"Automated infrastructure provisioning with CloudFormation and Terraform.", '
    '"Delivered Amazon Connect contact centre solution across 20+ airports."], '
    '"skills": "AWS, Terraform, Kubernetes, CI/CD, GitHub Actions, Docker, CloudWatch, Linux", '
    '"experience_headline": "Platform & Automation Engineer"}'
)

COVER_RESPONSE = (
    "Your role calls out Terraform across multi-account AWS environments, "
    "which matches my last three years of work. I have shipped Terraform "
    "modules across 15+ AWS accounts, cut deployment effort by 70% with "
    "CI/CD on GitHub Actions, and reduced AWS spend through rightsizing.\n\n"
    "The IAM and IaC patterns carry directly. Happy to walk through "
    "specifics.\n\n"
    "Sagar Verma\n"
    "sagarvd130@gmail.com | linkedin.com/in/sagarvbuilds | https://sagarverma.cv"
)


@pytest.fixture(autouse=True)
def setup_assets(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "profile.txt").write_text("Sagar Verma. AWS certified engineer.")
    import shutil
    src = Path("assets/SAGAR VERMA.docx")
    if src.exists():
        shutil.copy(src, assets / "SAGAR VERMA.docx")
    else:
        from docx import Document
        doc = Document()
        p = doc.add_paragraph("SAGAR VERMA")
        p.runs[0].bold = True
        doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
        doc.add_heading("WORK EXPERIENCE", level=1)
        doc.add_heading("Acme Corp", level=1)
        doc.add_paragraph("DevOps Engineer\t2020 - 2023")
        doc.add_paragraph("Maintained servers.", style="List Paragraph")
        doc.add_paragraph("Built pipelines.", style="List Paragraph")
        doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
        p2 = doc.add_paragraph("Skills: AWS, Docker")
        p2.runs[0].bold = True
        doc.save(assets / "SAGAR VERMA.docx")
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)
    # Profile loading lives in utils now; redirect + reset cache.
    monkeypatch.setattr("utils._PROFILE_PATH", assets / "profile.txt")
    monkeypatch.setattr("utils._profile_cache", None)


@pytest.mark.asyncio
async def test_tailor_returns_two_pdf_paths():
    import tailorer

    async def fake_lo(docx_path, out_dir):
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    # Both cover letter and resume tailoring now go through claude_cli on
    # the user's Max subscription. tailor() calls cover letter first, then
    # resume — so side_effect must match that order.
    with patch("tailorer.claude_complete", new=AsyncMock(side_effect=[COVER_RESPONSE, EXPANDED_RESPONSE])), \
         patch("tailorer._run_libreoffice", new=fake_lo), \
         patch("tailorer._libreoffice_path", "/fake/soffice"):
        tailorer._libreoffice_path = "/fake/soffice"
        resume_pdf, cover_pdf = await tailorer.tailor(JOB)
    assert resume_pdf.endswith(".pdf")
    assert cover_pdf.endswith(".pdf")
    assert "InteractivePtyLtd" in resume_pdf
    assert "CoverLetter" in cover_pdf


@pytest.mark.asyncio
async def test_tailor_resume_prompt_requests_all_four_sections():
    """The system rules sent to Claude must request summary, bullets, skills, experience_headline."""
    import tailorer
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return EXPANDED_RESPONSE

    with patch("tailorer.claude_complete", new=fake_complete):
        await tailorer._tailor_resume_text(JOB)

    system = captured["system"]
    assert "summary" in system
    assert "bullets" in system
    assert "skills" in system
    assert "experience_headline" in system


@pytest.mark.asyncio
async def test_tailor_resume_prompt_enforces_one_page_limits():
    """System rules must enforce word/bullet limits to prevent PDF overflow."""
    import tailorer
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return EXPANDED_RESPONSE

    with patch("tailorer.claude_complete", new=fake_complete):
        await tailorer._tailor_resume_text(JOB)

    system = captured["system"]
    assert "100 words" in system or "100-word" in system
    assert "8 bullets" in system


@pytest.mark.asyncio
async def test_export_resume_pdf_patches_skills_line(tmp_path, monkeypatch):
    """Skills paragraph (bold Normal starting with 'Skills:') is replaced."""
    from docx import Document
    import tailorer
    from pathlib import Path

    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)
    monkeypatch.setattr("tailorer._libreoffice_path", "/fake/soffice")

    doc = Document()
    p = doc.add_paragraph("SAGAR VERMA")
    p.runs[0].bold = True
    doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
    doc.add_heading("WORK EXPERIENCE", level=1)
    doc.add_heading("Acme Corp", level=1)
    doc.add_paragraph("DevOps Engineer\t2020 - 2023")
    doc.add_paragraph("Old bullet.", style="List Paragraph")
    doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
    p2 = doc.add_paragraph("Skills: OldSkill1, OldSkill2")
    p2.runs[0].bold = True
    doc.save(assets / "SAGAR VERMA.docx")

    sections = {
        "summary": "Experienced platform engineer.",
        "bullets": ["New bullet one.", "New bullet two."],
        "skills": "AWS, Terraform, Kubernetes",
        "experience_headline": "Platform Engineer",
    }

    saved_docx = None
    saved_copy = None

    async def fake_lo(docx_path, out_dir):
        nonlocal saved_docx, saved_copy
        saved_docx = docx_path
        import shutil
        saved_copy = str(docx_path) + ".bak"
        shutil.copy(docx_path, saved_copy)
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailorer._run_libreoffice", new=fake_lo):
        await tailorer._export_resume_pdf(sections, JOB)

    assert saved_copy is not None
    result_doc = Document(saved_copy)
    skills_paras = [p.text for p in result_doc.paragraphs if p.text.startswith("Skills:")]
    assert any("AWS" in t and "Terraform" in t for t in skills_paras)


VALID_LETTER = (
    "Your role calls out consolidating AWS accounts under one billing "
    "org, which is exactly the migration I led at my last employer.\n\n"
    "Over five years in cloud and DevOps I have shipped Terraform-managed "
    "multi-account environments, cut deployment effort by 70% with CI/CD "
    "pipelines on GitHub Actions, and reduced AWS spend by tens of "
    "thousands per month through rightsizing and Spot migration.\n\n"
    "The IAM design, cost-discipline, and IaC patterns from AWS translate "
    "directly to the work described. Happy to walk through specifics.\n\n"
    "Sagar Verma\n"
    "sagarvd130@gmail.com | linkedin.com/in/sagarvbuilds | https://sagarverma.cv"
)


def test_quality_gate_returns_none_for_valid_letter():
    import tailorer
    assert tailorer._quality_gate(VALID_LETTER) is None


def test_quality_gate_flags_refusal_with_marker_reason():
    """The exact production refusal must produce a refusal_marker reason."""
    import tailorer
    refusal = (
        "I cannot write this cover letter responsibly.\n"
        "The COMPANY-FACTS constraint (a hard rule) explicitly forbids me "
        "from making factual claims about the company. The job description "
        "is marked as 'Not available'. Please provide the full job "
        "description. Without the actual job description, I cannot:\n"
        "- Identify the core technologies the role demands\n"
        "Writing a cover letter without the JD would violate the "
        "TRUTHFULNESS and COMPANY-FACTS constraints."
    )
    reason = tailorer._quality_gate(refusal)
    assert reason is not None
    assert reason.startswith("refusal_marker:")


def test_quality_gate_flags_prompt_header_echo():
    """Haiku sometimes emits the system prompt's internal-step headers
    ('ADJACENCY DETECTION', 'JD core technologies') as the letter body."""
    import tailorer
    letter = (
        "ADJACENCY DETECTION\n\n"
        "JD core technologies:\n- SAP S/4HANA, SAP CPI, SAP BTP\n\n"
        "Profile core technologies:\n- AWS, Terraform, CI/CD\n\n"
        "Sagar Verma\nsagarvd130@gmail.com"
    )
    reason = tailorer._quality_gate(letter)
    assert reason is not None
    assert reason.startswith("meta_leak_marker:")


def test_quality_gate_flags_self_disqualifying_letter():
    """An actual Haiku output observed for a deeply-mismatched JD argued the
    candidate was not a fit and asked the recruiter to look elsewhere — must
    be flagged so the bot doesn't ship a withdrawal as an application."""
    import tailorer
    letter = (
        "I appreciate you sharing this opportunity, but I need to be direct: "
        "this role is not a good fit for my profile, and I won't write a "
        "cover letter that misrepresents my experience.\n\n"
        "The position calls for 10+ years as a SAP S/4HANA Integration "
        "Architect. My background is AWS cloud infrastructure, DevOps, and "
        "applied AI. I have no production experience with SAP, no S/4HANA "
        "integration work, and no track record in enterprise SAP "
        "landscapes.\n\n"
        "My honest assessment: your hiring team needs someone with direct "
        "SAP S/4HANA project delivery. That's not me.\n\n"
        "Best of luck filling this role.\n\n"
        "Sagar Verma\nsagarvd130@gmail.com"
    )
    reason = tailorer._quality_gate(letter)
    assert reason is not None
    assert reason.startswith("self_disqualify_marker:")


def test_quality_gate_flags_meta_leak_with_marker_reason():
    """An actual META_LEAK sample from the audit must trip the gate."""
    import tailorer
    leak = (
        "The job description is not available, so I will write based on "
        "what the role title \"Systems Engineer (Infrastructure & Cloud)\" "
        "at Idea 11 Pty Ltd typically demands, grounded only in what the "
        "profile contains. I will treat this as a STRONG MATCH given the "
        "candidate's direct AWS, Linux, IaC, CI/CD, and cloud operations "
        "background.\n\nRunning 200+ Linux servers across 15+ AWS accounts "
        "at 99.9% uptime is the kind of operational baseline the role "
        "implies.\n\nSagar Verma\nsagarvd130@gmail.com"
    )
    reason = tailorer._quality_gate(leak)
    assert reason is not None
    assert reason.startswith("meta_leak_marker:")


def test_quality_gate_flags_missing_signoff():
    import tailorer
    letter = "x" * 1000
    assert tailorer._quality_gate(letter) == "missing_signoff"


def test_quality_gate_flags_too_short():
    import tailorer
    letter = "Sagar Verma\nsome short text"
    reason = tailorer._quality_gate(letter)
    assert reason is not None
    assert reason.startswith("too_short:")


def test_quality_gate_flags_empty():
    import tailorer
    assert tailorer._quality_gate("") == "empty_output"


_REFUSAL_TEXT = (
    "I cannot write this cover letter responsibly. "
    "The COMPANY-FACTS constraint forbids me. "
    "Please provide the full job description."
)

_META_LEAK_TEXT = (
    "The job description is not available, so I will write based on "
    "what the role typically demands. I will treat this as a STRONG "
    "MATCH given the candidate's profile.\n\nRunning 200+ Linux servers "
    "across 15+ AWS accounts is the kind of work that defines this "
    "role. The candidate's profile shows five years of similar "
    "experience.\n\nSagar Verma\nsagarvd130@gmail.com"
)


@pytest.mark.asyncio
async def test_generate_cover_letter_raises_on_refusal():
    """LLM refusal must raise CoverLetterQualityError — never silently return."""
    import tailorer
    job = JobListing(
        url="https://au.seek.com/job/4",
        title="Senior Integration Engineer",
        company="INDEX Consultants",
        board="seek",
        description="A" * 500,
        easy_apply=True,
    )
    with patch("tailorer.claude_complete", new=AsyncMock(return_value=_REFUSAL_TEXT)):
        with pytest.raises(tailorer.CoverLetterQualityError) as excinfo:
            await tailorer._generate_cover_letter(job)
    assert excinfo.value.reason.startswith("refusal_marker:")
    assert excinfo.value.jd_len == 500


@pytest.mark.asyncio
async def test_generate_cover_letter_raises_on_meta_leak():
    """LLM meta-commentary must raise CoverLetterQualityError."""
    import tailorer
    job = JobListing(
        url="https://au.seek.com/job/5",
        title="Systems Engineer",
        company="Acme",
        board="seek",
        description="B" * 500,
        easy_apply=True,
    )
    with patch("tailorer.claude_complete", new=AsyncMock(return_value=_META_LEAK_TEXT)):
        with pytest.raises(tailorer.CoverLetterQualityError) as excinfo:
            await tailorer._generate_cover_letter(job)
    assert excinfo.value.reason.startswith("meta_leak_marker:")


@pytest.mark.asyncio
async def test_generate_cover_letter_calls_llm_even_with_empty_jd():
    """No template fallback: empty JD still goes through LLM. The user message
    must say JOB DESCRIPTION:(none) — no instruction-text the LLM would parrot."""
    import tailorer
    job = JobListing(
        url="https://au.seek.com/job/6",
        title="DevOps Engineer",
        company="Acme",
        board="seek",
        description="",
        easy_apply=True,
    )
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return VALID_LETTER

    with patch("tailorer.claude_complete", new=fake_complete):
        out = await tailorer._generate_cover_letter(job)

    assert out == VALID_LETTER
    assert "JOB DESCRIPTION:\n(none)" in captured["user"]
    assert "Not available. Write based on" not in captured["user"]


@pytest.mark.asyncio
async def test_generate_cover_letter_passes_through_valid_letter():
    """Valid LLM output is returned unchanged."""
    import tailorer
    job = JobListing(
        url="https://au.seek.com/job/5",
        title="Platform Engineer",
        company="Acme",
        board="seek",
        description="B" * 500,
        easy_apply=True,
    )
    valid_letter = (
        "Your role calls out Terraform across multi-account AWS environments, "
        "which matches my last three years of work. I have shipped Terraform "
        "modules across 15+ AWS accounts, cut deployment effort by 70% with "
        "CI/CD on GitHub Actions, and reduced AWS spend through rightsizing.\n\n"
        "The IAM and IaC patterns carry directly. Happy to walk through "
        "specifics.\n\n"
        "Sagar Verma\n"
        "sagarvd130@gmail.com | linkedin.com/in/sagarvbuilds | https://sagarverma.cv"
    )
    with patch("tailorer.claude_complete", new=AsyncMock(return_value=valid_letter)):
        out = await tailorer._generate_cover_letter(job)
    assert out == valid_letter


@pytest.mark.asyncio
async def test_export_resume_pdf_patches_experience_headline(tmp_path, monkeypatch):
    """Experience headline (Normal paragraph with tab + year) is updated."""
    from docx import Document
    import tailorer
    from pathlib import Path

    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)
    monkeypatch.setattr("tailorer._libreoffice_path", "/fake/soffice")

    doc = Document()
    p = doc.add_paragraph("SAGAR VERMA")
    p.runs[0].bold = True
    doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
    doc.add_heading("WORK EXPERIENCE", level=1)
    doc.add_heading("Acme Corp", level=1)
    doc.add_paragraph("DevOps Engineer\t2020 - 2023")
    doc.add_paragraph("Did stuff.", style="List Paragraph")
    doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
    p2 = doc.add_paragraph("Skills: AWS")
    p2.runs[0].bold = True
    doc.save(assets / "SAGAR VERMA.docx")

    sections = {
        "summary": "Experienced engineer.",
        "bullets": ["New bullet."],
        "skills": "AWS, Terraform",
        "experience_headline": "Platform & Automation Engineer",
    }

    saved_docx = None
    saved_copy = None

    async def fake_lo(docx_path, out_dir):
        nonlocal saved_docx, saved_copy
        saved_docx = docx_path
        import shutil
        saved_copy = str(docx_path) + ".bak"
        shutil.copy(docx_path, saved_copy)
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailorer._run_libreoffice", new=fake_lo):
        await tailorer._export_resume_pdf(sections, JOB)

    result_doc = Document(saved_copy)
    headlines = [p.text for p in result_doc.paragraphs if "\t" in p.text and any(c.isdigit() for c in p.text)]
    assert any("Platform & Automation Engineer" in h for h in headlines)
