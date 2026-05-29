"""Resume for TEG Senior MLE / AI Engineer role.

Built from scratch using python-docx (same approach as the FinXL resume).
Honest positioning: leads with the Applied AI Engineer block (production
GenAI + agent-based systems) and supports with 5+ years AWS production
engineering. Does NOT claim classical ML frameworks, Vertex AI, or model
training that aren't in profile.txt.
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


OUT_PDF_NAME = "SagarVerma_TEG_SeniorAIEngineer.pdf"
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

# Production AI / GenAI / agent-based positioning. Lead with the strongest
# JD-aligned credentials.
SUMMARY = (
    "Applied AI / Cloud Engineer with strong delivery in production GenAI "
    "and agent-based systems, backed by 5+ years of AWS cloud engineering "
    "at scale. Build and operate end-to-end Python services integrating "
    "Anthropic Claude (Sonnet, Haiku) and OpenAI APIs (gpt-4o-mini for "
    "classification and scoring), with multi-step agentic workflows, prompt "
    "caching, structured-output validation, and multi-provider model routing. "
    "Hands-on with FastAPI microservices, async SQLAlchemy / PostgreSQL, "
    "Docker, CI/CD on GitHub Actions, and AWS production at scale, with "
    "basic GCP exposure."
)

# Applied AI Engineer is the LEAD section for this role (most recent + most
# JD-relevant). Bullets ordered to match JD priorities: production AI, GenAI
# agents, scaling/cost, microservices/inference, MLOps practices.
APPLIED_AI_BULLETS = [
    # JD: production AI, GenAI/agent-based, recommendation/scoring use cases
    "Built and shipped production Python services integrating Anthropic Claude "
    "(Sonnet for generation, Haiku for low-latency validation) and OpenAI "
    "gpt-4o-mini for classification and scoring, driving end-to-end "
    "automation pipelines from input ingestion through structured output "
    "and downstream workflow execution.",

    # JD: GenAI and agent-based systems
    "Designed multi-step agentic workflows with bounded retries, recovery "
    "strategies, cumulative-failure tracking, and per-attempt structured "
    "logging, so long-running autonomous loops are debuggable, "
    "cost-controlled, and reliable in production.",

    # JD: scaling AI systems, cost / performance
    "Implemented Anthropic prompt caching (cache_control: ephemeral) on "
    "system and profile blocks to drive roughly 90% input-cost reduction on "
    "cached tokens for sustained-throughput LLM workloads.",

    # JD: APIs / microservices / batch and real-time inference
    "Delivered FastAPI backend services with async SQLAlchemy on PostgreSQL, "
    "S3 storage, OAuth identity, and a Stripe-ready billing surface, gating "
    "inbound documents through Claude Haiku validation before expensive "
    "downstream processing.",
]

PERIMATTIC_BULLETS = [
    # JD: cloud at scale, production AI deployment
    "Operated AWS production environments across 15+ accounts and 200+ Linux "
    "servers (Ubuntu, CentOS), maintaining 99.9% uptime through Infrastructure "
    "as Code (Terraform, CloudFormation) and automated provisioning.",

    # JD: container orchestration / inference systems
    "Delivered containerised production workloads on AWS EKS (managed "
    "Kubernetes), ECS, and Fargate using Docker, including service "
    "definitions, autoscaling, and rollout strategies.",

    # JD: MLOps practices, CI/CD, model lifecycle management
    "Built and operated automated CI/CD pipelines on AWS CodePipeline and "
    "GitHub Actions, reducing manual deployment effort by approximately 70% "
    "and improving release consistency across environments.",

    # JD: production AI / conversational AI experience (transferable)
    "Contributed to a production Amazon Connect contact-centre solution "
    "serving 20+ airports across 4+ countries using Amazon Lex v2 conversational "
    "AI, Lambda, DynamoDB, and Kinesis streams for conversation analytics.",

    # JD: monitoring frameworks
    "Implemented monitoring, alerting, and operational visibility using AWS "
    "CloudWatch, reducing downtime by approximately 20% across business-critical "
    "workloads.",

    # JD: collaboration with cross-functional teams (Data Science / engineering)
    "Collaborated with developers, project teams, and client stakeholders "
    "to take solutions from POC into production across international "
    "engagements in Agile / Scrum environments.",

    # JD: scalability / reliability / performance improvement
    "Reduced infrastructure costs by approximately US$4,000 per month through "
    "S3 analysis, lifecycle improvements, server consolidation, and rightsizing.",

    # JD: security and reliability of production systems
    "Hardened production AWS workloads with IAM least-privilege, VPC "
    "segmentation, security groups, KMS, Secrets Manager, CloudTrail, and "
    "GuardDuty across enterprise multi-account environments.",
]

LEADERSHIP_BULLETS = [
    "Led technology initiatives across 160+ clubs and campus facilities.",
    "Collaborated with stakeholders for governance and planning.",
]

# Skills line: GenAI/LLM/Python first, then cloud, then production patterns.
# Omits classical ML frameworks (sklearn/PyTorch/TF/Hugging Face) and Vertex AI
# since these are NOT in profile.txt.
SKILLS_LINE = (
    "Python, Anthropic Claude API, OpenAI API, GenAI, LLMs, Agentic Workflows, "
    "Multi-Step Agents, Prompt Caching, Prompt Engineering, Structured JSON, "
    "FastAPI, async SQLAlchemy, PostgreSQL, AWS, GCP (basic), Docker, Kubernetes, "
    "EKS, ECS, CI/CD, GitHub Actions, CloudWatch, Microservices, Production AI"
)


# ── Layout constants ────────────────────────────────────────────────────────
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

    # ── Header: name + clickable contact line ─────────────────────────────
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

    # ── Summary ────────────────────────────────────────────────────────────
    add_styled(doc, SUMMARY, size=BODY_PT, space_after=Pt(6))

    # ── Work experience: Applied AI FIRST (most recent + most JD-relevant) ─
    add_section_heading(doc, "WORK EXPERIENCE")
    add_styled(doc, "Applied AI Projects (Independent)", bold=True, size=BODY_PT,
               space_after=Pt(0))
    add_role_line(doc, "Applied AI Engineer", "Feb 2025 - Present")
    for b in APPLIED_AI_BULLETS:
        add_bullet(doc, b)

    # ── Perimattic ─────────────────────────────────────────────────────────
    add_styled(doc, "Perimattic", bold=True, size=BODY_PT, space_after=Pt(0))
    add_role_line(doc, "DevOps / Cloud Engineer", "Jul 2019 - Feb 2025")
    for b in PERIMATTIC_BULLETS:
        add_bullet(doc, b)

    # ── Leadership ─────────────────────────────────────────────────────────
    add_section_heading(doc, "LEADERSHIP EXPERIENCE")
    add_styled(doc, "ActivateUTS", bold=True, size=BODY_PT, space_after=Pt(0))
    add_role_line(doc, "Student Board Director", "Oct 2023 - Oct 2025")
    for b in LEADERSHIP_BULLETS:
        add_bullet(doc, b)

    # ── Education ──────────────────────────────────────────────────────────
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

    # ── Skills & Certifications ───────────────────────────────────────────
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
