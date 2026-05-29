"""Fresh-build resume for FinXL Associate DevOps Engineer.

Builds the docx from scratch using python-docx instead of cloning the
template. Every paragraph is constructed explicitly with the right style,
bullet formatting, and tab stops — no inheritance / index-shift issues.
"""
import subprocess
from pathlib import Path

import docx.opc.constants
from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT, WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Cm, RGBColor

import tailorer
from utils import detect_libreoffice


# Verified credly URLs from the existing template.
LINKEDIN_URL = "https://www.linkedin.com/in/sagarvbuilds/"
WEBSITE_URL = "https://sagarverma.cv"
CERT_CCP_URL = "https://www.credly.com/earner/earned/badge/6a392462-4d35-4440-bfc8-e4337a9046cb"
CERT_SAA_URL = "https://www.credly.com/badges/747774e9-90b8-4b14-9b0d-e551cc4219c1"


def add_hyperlink(paragraph, url: str, text: str, size_pt: float):
    """Append a clickable hyperlink to `paragraph` with explicit font/size.
    Self-contained: doesn't depend on existing runs for font inheritance."""
    part = paragraph.part
    r_id = part.relate_to(
        url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True,
    )

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")

    # Standard hyperlink styling: blue + underline.
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)

    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rPr.append(underline)

    # Explicit font (Times New Roman) and size — half-points, so 9.5pt = 19.
    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"), "Times New Roman")
    rFonts.set(qn("w:hAnsi"), "Times New Roman")
    rFonts.set(qn("w:cs"), "Times New Roman")
    rPr.append(rFonts)

    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))
    rPr.append(sz)

    run.append(rPr)
    text_el = OxmlElement("w:t")
    text_el.text = text
    text_el.set(qn("xml:space"), "preserve")
    run.append(text_el)

    hyperlink.append(run)
    paragraph._element.append(hyperlink)


OUT_PDF_NAME = "SagarVerma_FinXL_AssociateDevOpsEngineer.pdf"
OUT_DOCX = Path("output") / OUT_PDF_NAME.replace(".pdf", ".docx")
OUT_PDF = Path("output") / OUT_PDF_NAME

NAME = "SAGAR VERMA"
EMAIL = "sagarvd130@gmail.com"
PHONE = "+61 491621148"
LINKEDIN = "linkedin.com/in/sagarvbuilds"
WEBSITE = "https://sagarverma.cv"

SUMMARY = (
    "AWS-certified DevOps and Cloud Engineer with 5+ years of production "
    "experience automating provisioning across cloud and on-prem Linux "
    "infrastructure. Strong delivery in Python and Bash, Infrastructure "
    "as Code (Terraform, CloudFormation, Ansible), CI/CD (AWS CodePipeline, "
    "GitHub Actions), and container orchestration (Kubernetes / EKS, Docker, "
    "ECS). Basic Azure / GCP exposure, occasional Windows admin, and applied "
    "AI engineering with Anthropic Claude and OpenAI APIs."
)

PERIMATTIC_BULLETS = [
    "Operated AWS production environments across 15+ accounts with 200+ "
    "Linux servers (Ubuntu, CentOS) and occasional Windows server support, "
    "maintaining 99.9% uptime and automating provisioning end-to-end through "
    "Infrastructure as Code.",
    "Drove Infrastructure as Code adoption with Terraform and CloudFormation "
    "across multi-account AWS deployments, with familiarity with Ansible for "
    "configuration management and ad-hoc orchestration.",
    "Designed fault-tolerant AWS architectures using EC2, S3, RDS, Lambda, "
    "IAM, ALB, NLB, Auto Scaling, and Route 53, with basic exposure to "
    "Microsoft Azure and Google Cloud Platform for cross-cloud awareness.",
    "Delivered containerised production workloads on AWS EKS (managed "
    "Kubernetes), ECS, and Fargate using Docker, including service "
    "definitions, autoscaling, and rollout strategies.",
    "Built and operated automated CI/CD pipelines on AWS CodePipeline and "
    "GitHub Actions, reducing manual deployment effort by approximately 70% "
    "and improving release consistency across environments.",
    "Authored production Python and Bash automation for AWS Lambda, log "
    "parsing, deployment hooks, and operational tooling, with familiarity "
    "with PowerShell for cross-platform scripting needs.",
    "Hardened production AWS workloads with IAM least-privilege, VPC "
    "segmentation, security groups, KMS, Secrets Manager, CloudTrail, and "
    "GuardDuty across enterprise multi-account environments.",
    "Implemented monitoring, alerting, and operational visibility using AWS "
    "CloudWatch, with familiarity with Prometheus, Grafana, and ELK stack "
    "(Elasticsearch, Logstash, Kibana) for metrics and log aggregation in "
    "cloud-native environments.",
]

APPLIED_AI_BULLETS = [
    "Built end-to-end Python automation platforms integrating Anthropic "
    "Claude (Sonnet for generation, Haiku for low-latency validation) and "
    "OpenAI APIs, shipping multi-step agentic workflows with bounded "
    "retries, recovery strategies, and structured per-attempt logging.",
    "Implemented Anthropic prompt caching (cache_control: ephemeral) on "
    "system and profile blocks, achieving roughly 90% input-cost reduction "
    "on cached tokens for sustained-throughput LLM workloads.",
    "Engineered production browser automation with Playwright, persistent "
    "Chrome user-data-dirs, anti-detection flags, and session reuse across "
    "long-running autonomous cycles, including form-field automation across "
    "complex multi-step web workflows.",
    "Delivered FastAPI backend services with async SQLAlchemy on PostgreSQL, "
    "S3 storage, OAuth identity, GitHub Actions CI, and a typed Python "
    "codebase tested with pytest, end-to-end as a solo engineer.",
]

LEADERSHIP_BULLETS = [
    "Led technology initiatives across 160+ clubs and campus facilities.",
    "Collaborated with stakeholders for governance and planning.",
]

SKILLS_LINE = (
    "AWS, Azure (basic), GCP (basic), Linux (Ubuntu, CentOS), Windows Server "
    "Admin, Python, Bash, PowerShell, Terraform, CloudFormation, Ansible, "
    "Docker, Kubernetes, EKS, ECS, CI/CD, GitHub Actions, AWS CodePipeline, "
    "CloudWatch, Prometheus, Grafana, ELK, IAM, VPC, Networking, Security"
)


BODY_PT = 9.5  # was 10
ROLE_PT = 9.5
HEADING_PT = 11
SPACE_AFTER_BULLET = Pt(0.5)
SPACE_AFTER_PARAGRAPH = Pt(2)
SPACE_AFTER_SKILLS = Pt(1)
SPACE_BEFORE_SECTION = Pt(2)
SPACE_AFTER_SECTION = Pt(1)


def add_styled(doc, text, *, font="Times New Roman", size=BODY_PT,
               bold=False, italic=False, align=None, space_after=None):
    """Plain Normal-style paragraph with explicit run formatting."""
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    if space_after is not None:
        p.paragraph_format.space_after = space_after
    run = p.add_run(text)
    run.font.name = font
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    return p


def add_section_heading(doc, text):
    """Bold heading. No bottom rule — saves vertical space; the bold +
    caps treatment is enough to anchor the section visually."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = SPACE_BEFORE_SECTION
    p.paragraph_format.space_after = SPACE_AFTER_SECTION
    run = p.add_run(text)
    run.bold = True
    run.font.name = "Times New Roman"
    run.font.size = Pt(HEADING_PT)
    return p


def add_role_line(doc, role, dates):
    """Italic role line with right-aligned dates via tab stop."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = SPACE_AFTER_BULLET
    # Right tab stop near the page edge (page is ~21cm wide, margins ~1.5cm)
    p.paragraph_format.tab_stops.add_tab_stop(
        Cm(17.5), alignment=WD_TAB_ALIGNMENT.RIGHT,
    )
    run1 = p.add_run(role)
    run1.italic = True
    run1.font.name = "Times New Roman"
    run1.font.size = Pt(ROLE_PT)
    run2 = p.add_run("\t" + dates)
    run2.italic = True
    run2.font.name = "Times New Roman"
    run2.font.size = Pt(ROLE_PT)
    return p


def add_bullet(doc, text, *, italic=False):
    """Bullet using docx's built-in 'List Bullet' style; falls back to a
    manual bullet character if that style isn't available."""
    try:
        p = doc.add_paragraph(style="List Bullet")
    except KeyError:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.6)
        p.add_run("• ")
    p.paragraph_format.space_after = SPACE_AFTER_BULLET
    run = p.add_run(text)
    run.italic = italic
    run.font.name = "Times New Roman"
    run.font.size = Pt(BODY_PT)
    return p


def add_skills_line(doc, label, content):
    """Bold label, regular content, indented continuation if it wraps."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = SPACE_AFTER_SKILLS
    label_run = p.add_run(f"{label}: ")
    label_run.bold = True
    label_run.font.name = "Times New Roman"
    label_run.font.size = Pt(BODY_PT)
    body_run = p.add_run(content)
    body_run.font.name = "Times New Roman"
    body_run.font.size = Pt(BODY_PT)
    return p


def render():
    tailorer._libreoffice_path = detect_libreoffice()

    doc = Document()

    # Tighter margins to maximise content density on a single page.
    for section in doc.sections:
        section.top_margin = Cm(1.3)
        section.bottom_margin = Cm(1.3)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)

    # ── Header: name + contact ─────────────────────────────────────────
    name_para = doc.add_paragraph()
    name_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_para.paragraph_format.space_after = Pt(2)
    name_run = name_para.add_run(NAME)
    name_run.bold = True
    name_run.font.name = "Times New Roman"
    name_run.font.size = Pt(18)

    contact_para = doc.add_paragraph()
    contact_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_para.paragraph_format.space_after = Pt(6)

    def _add_plain(text):
        r = contact_para.add_run(text)
        r.font.name = "Times New Roman"
        r.font.size = Pt(BODY_PT)

    add_hyperlink(contact_para, f"mailto:{EMAIL}", EMAIL, BODY_PT)
    _add_plain(f" | {PHONE} | ")
    add_hyperlink(contact_para, LINKEDIN_URL, LINKEDIN, BODY_PT)
    _add_plain(" | ")
    add_hyperlink(contact_para, WEBSITE_URL, WEBSITE, BODY_PT)

    # ── Summary ────────────────────────────────────────────────────────
    add_styled(doc, SUMMARY, size=10, space_after=Pt(6))

    # ── Work experience: Perimattic ────────────────────────────────────
    add_section_heading(doc, "WORK EXPERIENCE")
    add_styled(doc, "Perimattic", bold=True, size=10, space_after=Pt(0))
    add_role_line(doc, "DevOps / Cloud Engineer", "Jul 2019 - Feb 2025")
    for b in PERIMATTIC_BULLETS:
        add_bullet(doc, b)

    # ── Work experience: Applied AI (Feb 2025 - Present) ───────────────
    add_styled(doc, "Applied AI Projects (Independent)",
               bold=True, size=10, space_after=Pt(0))
    add_role_line(doc, "Applied AI Engineer", "Feb 2025 - Present")
    for b in APPLIED_AI_BULLETS:
        add_bullet(doc, b)

    # ── Leadership ─────────────────────────────────────────────────────
    add_section_heading(doc, "LEADERSHIP EXPERIENCE")
    add_styled(doc, "ActivateUTS", bold=True, size=10, space_after=Pt(0))
    add_role_line(doc, "Student Board Director", "Oct 2023 - Oct 2025")
    for b in LEADERSHIP_BULLETS:
        add_bullet(doc, b)

    # ── Education ──────────────────────────────────────────────────────
    add_section_heading(doc, "EDUCATION")

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.tab_stops.add_tab_stop(
        Cm(16), alignment=WD_TAB_ALIGNMENT.RIGHT,
    )
    r = p.add_run("University of Technology Sydney")
    r.bold = True; r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    r = p.add_run("\tSydney, Australia")
    r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    add_role_line(doc, "Master of Information Technology (Internetworking)",
                  "Jul 2023 - Jul 2025")

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.tab_stops.add_tab_stop(
        Cm(16), alignment=WD_TAB_ALIGNMENT.RIGHT,
    )
    r = p.add_run("GLA University")
    r.bold = True; r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    r = p.add_run("\tMathura, India")
    r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    add_role_line(doc, "Bachelor of Technology (Computer Science Engineering)",
                  "Aug 2015 - Jul 2019")

    # ── Skills & Certifications ────────────────────────────────────────
    add_section_heading(doc, "SKILLS & CERTIFICATIONS")
    add_skills_line(doc, "Skills", SKILLS_LINE)

    # Certifications line with each cert linking to its credly badge.
    cert_para = doc.add_paragraph()
    cert_para.paragraph_format.space_after = SPACE_AFTER_SKILLS
    label_run = cert_para.add_run("Certifications: ")
    label_run.bold = True
    label_run.font.name = "Times New Roman"
    label_run.font.size = Pt(BODY_PT)
    add_hyperlink(cert_para, CERT_CCP_URL, "AWS Certified Cloud Practitioner", BODY_PT)
    sep = cert_para.add_run(", ")
    sep.font.name = "Times New Roman"; sep.font.size = Pt(BODY_PT)
    add_hyperlink(cert_para, CERT_SAA_URL, "AWS Certified Solutions Architect (Associate)", BODY_PT)
    end = cert_para.add_run(".")
    end.font.name = "Times New Roman"; end.font.size = Pt(BODY_PT)

    # ── Save and convert ───────────────────────────────────────────────
    OUT_DOCX.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT_DOCX)

    result = subprocess.run(
        [tailorer._libreoffice_path, "--headless", "--convert-to", "pdf",
         "--outdir", str(OUT_PDF.parent), str(OUT_DOCX)],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice error: {result.stderr.decode()}")

    if not OUT_PDF.exists():
        raise RuntimeError(f"LibreOffice did not produce {OUT_PDF}")
    return str(OUT_PDF)


if __name__ == "__main__":
    pdf_path = render()
    print(f"Resume PDF: {pdf_path}")
