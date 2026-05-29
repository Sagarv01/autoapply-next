"""Resume for a Senior Data Engineer role (banking / financial services,
cloud-native AWS data platform, AI/ML-ready data).

Built from scratch with python-docx (same approach as the TEG / FinXL resumes).

Honest positioning: this is a Cloud/Platform engineer with applied-AI and
data-pipeline experience, NOT a PySpark/Java/Kafka data engineer. The JD's hard
must-haves (PySpark, Java, Kafka, AWS Glue, RAG) are NOT in profile.txt, so they
are listed as "familiar" (per Sagar's reframing preference) and never claimed as
hands-on strengths. The resume leads with the genuinely strong, transferable
assets: DynamoDB data modelling, Kinesis event-streaming pipelines, Python
pipelines, RESTful APIs (FastAPI), RDS/PostgreSQL, CI/CD, AWS data-governance,
and AI/ML data-pipeline work (ingestion, validation, data-quality gating).
"""
import subprocess
from pathlib import Path

import docx.opc.constants
from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT, WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Cm

import tailorer
from utils import detect_libreoffice


OUT_PDF_NAME = "SagarVerma_SeniorDataEngineer.pdf"
OUT_DOCX = Path("output") / OUT_PDF_NAME.replace(".pdf", ".docx")
OUT_PDF = Path("output") / OUT_PDF_NAME

NAME = "SAGAR VERMA"
EMAIL = "sagarvd130@gmail.com"
PHONE = "+61 491621148"
LINKEDIN = "linkedin.com/in/sagarvbuilds"
WEBSITE = "https://sagarverma.cv"
LINKEDIN_URL = "https://www.linkedin.com/in/sagarvbuilds/"
WEBSITE_URL = "https://sagarverma.cv"
CERT_CCP_URL = "https://www.credly.com/earner/earned/badge/6a392462-4d35-4440-bfc8-e4337a9046cb"
CERT_SAA_URL = "https://www.credly.com/badges/747774e9-90b8-4b14-9b0d-e551cc4219c1"

# Data-platform positioning. Lead with AWS data + Python pipelines + event
# streaming, then AI/ML-ready data work, then regulated-environment governance.
SUMMARY = (
    "AWS Cloud and Data Engineer with 5+ years building and operating "
    "production data and platform services on AWS at scale. Strong in Python "
    "data pipelines, DynamoDB data modelling, and Kinesis event-streaming "
    "pipelines feeding near real-time analytics, with hands-on RESTful API "
    "delivery (FastAPI) and end-to-end CI/CD on GitHub Actions. Recent focus on "
    "AI/ML-ready data pipelines: document ingestion, parsing, structured-output "
    "validation, and data-quality gating for LLM workloads. Experienced "
    "delivering in regulated, security-sensitive, multi-account environments "
    "with strong IAM, KMS, audit-logging, and data-governance controls."
)

# Applied AI / Data Pipelines is the LEAD section (most recent, most JD-relevant
# for the AI/ML-ready data, data-quality, and MLOps-practice requirements).
APPLIED_AI_BULLETS = [
    # JD: scalable data pipelines, data quality, ingestion
    "Built end-to-end Python data pipelines that ingest, parse, and validate "
    "structured and semi-structured inputs (including PDF extraction with "
    "pdfplumber), enforce data-quality gates, and feed downstream LLM and "
    "automation workflows.",

    # JD: AI/ML-ready datasets, data quality
    "Engineered AI-ready data flows with structured-output validation and "
    "parser repair, using Claude Haiku to gate low-quality inputs before "
    "expensive downstream processing, improving dataset quality and controlling "
    "cost.",

    # JD: MLOps practices (reproducible pipelines, versioning, monitoring)
    "Designed reproducible, monitored automation pipelines with bounded "
    "retries, recovery strategies, cumulative-failure tracking, and "
    "per-attempt structured logging, giving long-running data workflows "
    "observability and repeatability.",

    # JD: RESTful APIs, storage, security controls
    "Delivered RESTful API services with FastAPI, async SQLAlchemy on "
    "PostgreSQL, and S3 storage, with OAuth identity and environment-based "
    "secrets handling, maintained as typed Python with pytest and GitHub "
    "Actions CI.",
]

PERIMATTIC_BULLETS = [
    # JD: event-driven architectures, distributed data processing, real-time
    # (strongest transferable data-engineering asset)
    "Engineered Kinesis event-streaming pipelines consuming Amazon Connect "
    "Contact Trace Record (CTR) and Agent Event streams, powering near "
    "real-time reporting and operational analytics for a contact-centre "
    "platform serving 20+ airports across 4+ countries.",

    # JD: AWS data services, DynamoDB, data modelling, storage
    "Built and operated DynamoDB data models and S3 / RDS (PostgreSQL, MySQL, "
    "Aurora) storage for production systems across 15+ AWS accounts, with "
    "ElastiCache for Redis caching hot read paths.",

    # JD: event-driven, RESTful APIs, Python
    "Developed Python (Lambda) services for event-driven processing and "
    "integration across SNS / SQS messaging and API Gateway REST endpoints.",

    # JD: CI/CD, DevOps, SDLC, auditability
    "Drove Infrastructure as Code (Terraform, CloudFormation) and CI/CD "
    "(AWS CodePipeline, GitHub Actions), cutting manual deployment effort by "
    "roughly 70% and enabling reproducible, auditable deployments across "
    "environments.",

    # JD: data governance, privacy, auditability in regulated environments
    "Applied data-governance and security controls in regulated, multi-account "
    "environments: IAM least-privilege, KMS encryption, Secrets Manager, "
    "CloudTrail audit logging, GuardDuty, AWS Config, and Security Hub.",

    # JD: scalability / cost efficiency of pipelines and storage
    "Reduced infrastructure cost by roughly US$4,000 per month through S3 "
    "lifecycle optimisation, storage analysis, rightsizing, and server "
    "consolidation.",

    # JD: resilience, reliability, monitoring
    "Operated 200+ Linux servers (Ubuntu, CentOS) at 99.9% uptime with "
    "CloudWatch monitoring and alerting, reducing downtime by approximately "
    "20% across business-critical workloads.",

    # JD: collaboration, influence, communication
    "Collaborated with developers, architecture, and client stakeholders to "
    "take solutions from proof of concept into production across international "
    "engagements in Agile / Scrum environments.",
]

LEADERSHIP_BULLETS = [
    "Led technology initiatives across 160+ clubs and campus facilities.",
    "Collaborated with stakeholders for governance and planning.",
]

# Skills line: genuine strengths FIRST, reframed gaps (PySpark, Java, Kafka,
# Glue, RAG) marked "familiar" per Sagar's preference, never claimed hands-on.
SKILLS_LINE = (
    "Python, SQL, AWS, DynamoDB, Kinesis, Lambda, S3, RDS (PostgreSQL, MySQL, "
    "Aurora), Event-Driven Architecture, RESTful APIs, FastAPI, Data Modelling, "
    "Data Pipelines, ETL, Data Quality, Data Governance, CI/CD, GitHub Actions, "
    "Terraform, CloudFormation, Docker, Kubernetes, CloudWatch, IAM, KMS, "
    "LLM / AI Data Pipelines, MLOps practices, Agile / Scrum; "
    "PySpark, Java, Kafka, AWS Glue, RAG (familiar)"
)


# -- Layout constants --------------------------------------------------------
BODY_PT = 9.5
ROLE_PT = 9.5
HEADING_PT = 11
SPACE_AFTER_BULLET = Pt(0.5)
SPACE_AFTER_SKILLS = Pt(1)
SPACE_BEFORE_SECTION = Pt(2)
SPACE_AFTER_SECTION = Pt(1)


def add_hyperlink(paragraph, url: str, text: str, size_pt: float):
    """Append a clickable hyperlink to `paragraph` with explicit font/size."""
    part = paragraph.part
    r_id = part.relate_to(
        url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); rPr.append(color)
    underline = OxmlElement("w:u"); underline.set(qn("w:val"), "single"); rPr.append(underline)
    rFonts = OxmlElement("w:rFonts")
    for k in ("w:ascii", "w:hAnsi", "w:cs"):
        rFonts.set(qn(k), "Times New Roman")
    rPr.append(rFonts)
    sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(size_pt * 2))); rPr.append(sz)
    run.append(rPr)
    text_el = OxmlElement("w:t")
    text_el.text = text
    text_el.set(qn("xml:space"), "preserve")
    run.append(text_el)
    hyperlink.append(run)
    paragraph._element.append(hyperlink)


def add_styled(doc, text, *, size=BODY_PT, bold=False, italic=False,
               align=None, space_after=None):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    if space_after is not None:
        p.paragraph_format.space_after = space_after
    run = p.add_run(text)
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    return p


def add_section_heading(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = SPACE_BEFORE_SECTION
    p.paragraph_format.space_after = SPACE_AFTER_SECTION
    run = p.add_run(text)
    run.bold = True
    run.font.name = "Times New Roman"
    run.font.size = Pt(HEADING_PT)
    return p


def add_role_line(doc, role, dates):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = SPACE_AFTER_BULLET
    p.paragraph_format.tab_stops.add_tab_stop(
        Cm(17.5), alignment=WD_TAB_ALIGNMENT.RIGHT,
    )
    r1 = p.add_run(role); r1.italic = True
    r1.font.name = "Times New Roman"; r1.font.size = Pt(ROLE_PT)
    r2 = p.add_run("\t" + dates); r2.italic = True
    r2.font.name = "Times New Roman"; r2.font.size = Pt(ROLE_PT)
    return p


def add_bullet(doc, text):
    try:
        p = doc.add_paragraph(style="List Bullet")
    except KeyError:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.6)
        p.add_run("• ")
    p.paragraph_format.space_after = SPACE_AFTER_BULLET
    run = p.add_run(text)
    run.font.name = "Times New Roman"
    run.font.size = Pt(BODY_PT)
    return p


def add_skills_line(doc, label, content):
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

    for section in doc.sections:
        section.top_margin = Cm(1.3)
        section.bottom_margin = Cm(1.3)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)

    # -- Header: name + clickable contact line --------------------------------
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

    def _plain(text):
        r = contact_para.add_run(text)
        r.font.name = "Times New Roman"
        r.font.size = Pt(BODY_PT)

    add_hyperlink(contact_para, f"mailto:{EMAIL}", EMAIL, BODY_PT)
    _plain(f" | {PHONE} | ")
    add_hyperlink(contact_para, LINKEDIN_URL, LINKEDIN, BODY_PT)
    _plain(" | ")
    add_hyperlink(contact_para, WEBSITE_URL, WEBSITE, BODY_PT)

    # -- Summary --------------------------------------------------------------
    add_styled(doc, SUMMARY, size=BODY_PT, space_after=Pt(6))

    # -- Work experience: Applied AI / Data Pipelines FIRST -------------------
    add_section_heading(doc, "WORK EXPERIENCE")
    add_styled(doc, "Applied AI & Data Pipelines (Independent)", bold=True,
               size=BODY_PT, space_after=Pt(0))
    add_role_line(doc, "Applied AI / Data Engineer", "Feb 2025 - Present")
    for b in APPLIED_AI_BULLETS:
        add_bullet(doc, b)

    # -- Perimattic -----------------------------------------------------------
    add_styled(doc, "Perimattic", bold=True, size=BODY_PT, space_after=Pt(0))
    add_role_line(doc, "Cloud / Data Engineer", "Jul 2019 - Feb 2025")
    for b in PERIMATTIC_BULLETS:
        add_bullet(doc, b)

    # -- Leadership -----------------------------------------------------------
    add_section_heading(doc, "LEADERSHIP EXPERIENCE")
    add_styled(doc, "ActivateUTS", bold=True, size=BODY_PT, space_after=Pt(0))
    add_role_line(doc, "Student Board Director", "Oct 2023 - Oct 2025")
    for b in LEADERSHIP_BULLETS:
        add_bullet(doc, b)

    # -- Education ------------------------------------------------------------
    add_section_heading(doc, "EDUCATION")

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.tab_stops.add_tab_stop(Cm(17.5), alignment=WD_TAB_ALIGNMENT.RIGHT)
    r = p.add_run("University of Technology Sydney")
    r.bold = True; r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    r = p.add_run("\tSydney, Australia")
    r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    add_role_line(doc, "Master of Information Technology (Internetworking)",
                  "Jul 2023 - Jul 2025")

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.tab_stops.add_tab_stop(Cm(17.5), alignment=WD_TAB_ALIGNMENT.RIGHT)
    r = p.add_run("GLA University")
    r.bold = True; r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    r = p.add_run("\tMathura, India")
    r.font.name = "Times New Roman"; r.font.size = Pt(BODY_PT)
    add_role_line(doc, "Bachelor of Technology (Computer Science Engineering)",
                  "Aug 2015 - Jul 2019")

    # -- Skills & Certifications ---------------------------------------------
    add_section_heading(doc, "SKILLS & CERTIFICATIONS")
    add_skills_line(doc, "Skills", SKILLS_LINE)

    cert_para = doc.add_paragraph()
    cert_para.paragraph_format.space_after = SPACE_AFTER_SKILLS
    label_run = cert_para.add_run("Certifications: ")
    label_run.bold = True
    label_run.font.name = "Times New Roman"
    label_run.font.size = Pt(BODY_PT)
    add_hyperlink(cert_para, CERT_CCP_URL,
                  "AWS Certified Cloud Practitioner", BODY_PT)
    sep = cert_para.add_run(", ")
    sep.font.name = "Times New Roman"; sep.font.size = Pt(BODY_PT)
    add_hyperlink(cert_para, CERT_SAA_URL,
                  "AWS Certified Solutions Architect (Associate)", BODY_PT)
    end = cert_para.add_run(".")
    end.font.name = "Times New Roman"; end.font.size = Pt(BODY_PT)

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
