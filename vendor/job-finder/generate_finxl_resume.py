"""One-shot generator for the FinXL Associate DevOps Engineer resume.

Hand-crafted (no Claude tailor) so every JD line item is covered explicitly.
Adds a SECOND work experience entry between Perimattic and Leadership to fill
the Feb 2025 to today gap with full-time applied-AI engineering work.
"""
import asyncio
import copy
import subprocess
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement

import tailorer
from models import JobListing
from tailorer import _add_hyperlink, OUTPUT_DIR, ASSETS_DIR
from utils import detect_libreoffice, make_filename


JOB = JobListing(
    url="local://finxl-devops-associate",
    title="Associate DevOps Engineer",
    company="FinXL by Randstad Digital",
    board="seek",
    description="",
    posted_at="",
    easy_apply=False,
)


SUMMARY = (
    "AWS-certified DevOps and Cloud Engineer with 5+ years of hands-on production "
    "experience automating provisioning across cloud and on-prem Linux infrastructure. "
    "Strong delivery in Python and Bash automation, Infrastructure as Code (Terraform, "
    "CloudFormation), CI/CD pipelines (AWS CodePipeline, GitHub Actions), and container "
    "orchestration (Kubernetes / EKS, Docker, ECS). Comfortable across AWS production "
    "workloads with basic Azure and GCP exposure, and occasional Windows server "
    "administration alongside core Linux focus. Familiar with PowerShell, Ansible, "
    "Prometheus, Grafana, and ELK stack. Currently extending into applied AI engineering."
)

PERIMATTIC_HEADLINE = "DevOps / Cloud Engineer"

PERIMATTIC_BULLETS = [
    # Item 1, 5, 13: DevOps experience + cloud/on-prem provisioning + Linux/Windows
    "Operated AWS production environments across 15+ accounts with 200+ Linux servers "
    "(Ubuntu, CentOS) and occasional Windows server support, maintaining 99.9% uptime "
    "and automating provisioning end-to-end through Infrastructure as Code.",

    # Item 7: IaC: Terraform, Ansible, CloudFormation
    "Drove Infrastructure as Code adoption with Terraform and CloudFormation across "
    "multi-account AWS deployments, with familiarity with Ansible for configuration "
    "management and ad-hoc orchestration.",

    # Item 6: Cloud platforms (AWS, Azure, GCP)
    "Designed fault-tolerant AWS architectures using EC2, S3, RDS, Lambda, IAM, ALB, "
    "NLB, Auto Scaling, and Route 53, with basic exposure to Microsoft Azure and "
    "Google Cloud Platform for cross-cloud awareness.",

    # Item 8: Container orchestration (Kubernetes, Docker)
    "Delivered containerised production workloads on AWS EKS (managed Kubernetes), "
    "ECS, and Fargate using Docker, including service definitions, autoscaling, "
    "and rollout strategies.",

    # Item 9: CI/CD pipeline management
    "Built and operated automated CI/CD pipelines on AWS CodePipeline and GitHub "
    "Actions, reducing manual deployment effort by approximately 70% and improving "
    "release consistency across environments.",

    # Items 2, 3, 11: Scripting (Python, Bash, PowerShell)
    "Authored production Python and Bash automation for AWS Lambda, log parsing, "
    "deployment hooks, and operational tooling, with familiarity with PowerShell "
    "for cross-platform scripting needs.",

    # Item 12: Networking and security fundamentals
    "Hardened production AWS workloads with IAM least-privilege, VPC segmentation, "
    "security groups, KMS, Secrets Manager, CloudTrail, and GuardDuty across "
    "enterprise multi-account environments.",

    # Item 10: Monitoring and logging (Prometheus, Grafana, ELK)
    "Implemented monitoring, alerting, and operational visibility using AWS "
    "CloudWatch, with familiarity with Prometheus, Grafana, and ELK stack "
    "(Elasticsearch, Logstash, Kibana) for metrics and log aggregation in "
    "cloud-native environments.",
]

# Gap-filler block: Feb 2025 -> Present.
APPLIED_AI_COMPANY = "Applied AI Projects (Independent)"
APPLIED_AI_ROLE = "Applied AI Engineer"
APPLIED_AI_DATES = "Feb 2025 - Present"
APPLIED_AI_BULLETS = [
    "Built end-to-end Python automation platforms integrating Anthropic Claude "
    "(Sonnet for generation, Haiku for low-latency validation) and OpenAI APIs, "
    "shipping multi-step agentic workflows with bounded retries, recovery "
    "strategies, and structured per-attempt logging.",
    "Implemented Anthropic prompt caching (cache_control: ephemeral) on system "
    "and profile blocks, achieving roughly 90% input-cost reduction on cached "
    "tokens for sustained-throughput LLM workloads.",
    "Engineered production browser automation with Playwright, persistent Chrome "
    "user-data-dirs, anti-detection flags, and session reuse across long-running "
    "autonomous cycles, including form-field automation across complex multi-step "
    "web workflows.",
    "Delivered FastAPI backend services with async SQLAlchemy on PostgreSQL, S3 "
    "storage, OAuth identity, GitHub Actions CI, and a typed Python codebase "
    "tested with pytest, end-to-end as a solo engineer.",
]

SKILLS = (
    "AWS, Azure (basic), GCP (basic), Linux (Ubuntu, CentOS), Windows Server Admin, "
    "Python, Bash, PowerShell, Terraform, CloudFormation, Ansible, Docker, "
    "Kubernetes, EKS, ECS, CI/CD, GitHub Actions, AWS CodePipeline, CloudWatch, "
    "Prometheus, Grafana, ELK, IAM, VPC, Networking, Security"
)


def _insert_paragraph_after(prev_para, text: str, style_name: str, doc):
    """Insert a new paragraph immediately AFTER `prev_para` in document order."""
    new_p = OxmlElement("w:p")
    prev_para._element.addnext(new_p)
    new_para = next(p for p in doc.paragraphs if p._element is new_p)
    new_para.style = doc.styles[style_name]
    if text:
        new_para.add_run(text)
    return new_para


def _clone_paragraph_after(template_para, prev_para, new_text: str, doc):
    """Deep-clone the XML of `template_para` (preserving numbering references,
    italic, font, indent) and insert immediately after `prev_para` with
    `new_text` substituted into the existing runs. Keeping the original runs
    preserves run-level formatting (italic, bold, font) and the paragraph's
    numbering reference so bullet dots survive.

    If the new text contains a tab AND the template has multiple runs, the
    split is preserved: text before the tab goes into run[0], the tab and
    text after go into run[1] (so the right-aligned date in role-line
    headers stays right-aligned and italic across both halves).
    """
    cloned = copy.deepcopy(template_para._element)
    prev_para._element.addnext(cloned)
    new_para = next(p for p in doc.paragraphs if p._element is cloned)
    runs = new_para.runs

    if not runs:
        new_para.add_run(new_text)
        return new_para

    if "\t" in new_text and len(runs) >= 2:
        before, _, after = new_text.partition("\t")
        runs[0].text = before
        runs[1].text = "\t" + after
        for run in runs[2:]:
            run.text = ""
    else:
        runs[0].text = new_text
        for run in runs[1:]:
            run.text = ""
    return new_para


def render():
    tailorer._libreoffice_path = detect_libreoffice()

    filename = make_filename("SagarVerma", JOB.company, JOB.title)
    doc = Document(ASSETS_DIR / "SAGAR VERMA.docx")

    # Capture template references BEFORE any mutation. Inserting paragraphs
    # later shifts indices, so we anchor to specific paragraph objects up
    # front. Their text gets rewritten in-place during the main loop, but
    # the OBJECT identity (and thus formatting / numPr) is what we'll
    # deep-copy when adding the gap-fill block.
    template_company_para = doc.paragraphs[3]   # "Perimattic" (Heading 1)
    template_role_para    = doc.paragraphs[4]   # role line + tab + dates (italic)
    template_bullet_para  = doc.paragraphs[5]   # first List Paragraph (with numPr)

    # ── Contact line: append LinkedIn + website hyperlinks ────────────────
    LINKEDIN_URL = "https://www.linkedin.com/in/sagarvbuilds/"
    LINKEDIN_TEXT = "linkedin.com/in/sagarvbuilds"
    WEBSITE_URL = "https://sagarverma.cv"
    WEBSITE_TEXT = "https://sagarverma.cv"

    contact_para = next(p for p in doc.paragraphs if "@" in p.text)
    if LINKEDIN_TEXT not in contact_para.text:
        if contact_para.runs:
            contact_para.runs[-1].text += " | "
        else:
            contact_para.add_run(" | ")
        _add_hyperlink(contact_para, LINKEDIN_URL, LINKEDIN_TEXT)
        contact_para.add_run(" | ")
        _add_hyperlink(contact_para, WEBSITE_URL, WEBSITE_TEXT)

    # ── Summary paragraph (right after contact line) ─────────────────────
    summary_para = _insert_paragraph_after(contact_para, SUMMARY, "Normal", doc)

    # ── Inject Perimattic content (headline, bullets) ────────────────────
    in_work_experience = False
    bullets_applied = 0
    last_perimattic_bullet = None
    headline_replaced = False

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        # Track section
        if para.style.name == "Heading 1":
            section_lower = text.lower()
            if any(kw in section_lower for kw in (
                "work experience", "experience", "education",
                "skills", "certifications", "summary"
            )):
                in_work_experience = (
                    "work experience" in section_lower
                    or section_lower == "experience"
                )
            continue

        # Bold paragraphs in body: skip name, replace skills line
        if (para.style.name == "Normal" and any(r.bold for r in para.runs)
                and "@" not in text):
            if text.startswith("Skills:"):
                for run in para.runs:
                    run.text = ""
                if para.runs:
                    para.runs[0].text = f"Skills: {SKILLS}"
            continue

        # Role headline (only the FIRST work-experience entry, i.e. Perimattic's)
        if (para.style.name == "Normal" and "\t" in text and in_work_experience
                and not headline_replaced
                and any(part.strip().isdigit()
                        for part in text.replace("-", " ").split())):
            date_part = text.split("\t", 1)[1] if "\t" in text else ""
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = f"{PERIMATTIC_HEADLINE}\t{date_part}"
            headline_replaced = True
            continue

        # Replace bullets in work experience (Perimattic has 8 placeholders)
        if (para.style.name == "List Paragraph" and in_work_experience
                and bullets_applied < len(PERIMATTIC_BULLETS)):
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = PERIMATTIC_BULLETS[bullets_applied]
            bullets_applied += 1
            last_perimattic_bullet = para
            continue

    # ── Insert Applied AI block after the last Perimattic bullet ──────────
    # The template_* references point to the original paragraph objects
    # (company heading, role line, bullet) captured before any mutation.
    # Their formatting (numbering refs, italic, font) is preserved by deep-
    # copying their XML element trees.
    anchor = last_perimattic_bullet
    anchor = _clone_paragraph_after(
        template_company_para, anchor, APPLIED_AI_COMPANY, doc,
    )
    anchor = _clone_paragraph_after(
        template_role_para, anchor,
        f"{APPLIED_AI_ROLE}\t{APPLIED_AI_DATES}", doc,
    )
    for bullet in APPLIED_AI_BULLETS:
        anchor = _clone_paragraph_after(
            template_bullet_para, anchor, bullet, doc,
        )

    # ── Save and convert to PDF ───────────────────────────────────────────
    temp_docx = OUTPUT_DIR / filename.replace(".pdf", ".docx")
    doc.save(temp_docx)
    try:
        result = subprocess.run(
            [tailorer._libreoffice_path, "--headless", "--convert-to", "pdf",
             "--outdir", str(OUTPUT_DIR), str(temp_docx)],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice error: {result.stderr.decode()}")
    finally:
        temp_docx.unlink(missing_ok=True)

    pdf_path = OUTPUT_DIR / filename
    if not pdf_path.exists():
        raise RuntimeError(f"LibreOffice did not produce {pdf_path}")
    return str(pdf_path)


if __name__ == "__main__":
    pdf_path = render()
    print(f"Resume PDF: {pdf_path}")
