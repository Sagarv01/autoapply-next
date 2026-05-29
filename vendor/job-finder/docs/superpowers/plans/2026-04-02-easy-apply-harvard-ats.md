# Easy Apply + Harvard ATS Tailoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter Seek to Easy Apply jobs only, lower match threshold to 45%, and expand resume/cover letter tailoring with Harvard ATS criteria across summary, bullets, skills, and experience headline — keeping the PDF to one page.

**Architecture:** Four independent changes applied in order: model field → scraper detection → worker filter → tailorer expansion. Each is independently testable. The tailorer change is the most complex: it expands the Claude prompt schema and rewrites four DOCX sections, fixing a pre-existing bug where the name paragraph was being overwritten.

**Tech Stack:** Python 3.13, python-docx, anthropic SDK, pytest, playwright (tests mocked)

---

### Task 1: Add `easy_apply` field to `JobListing`

**Files:**
- Modify: `models.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scraper.py`:

```python
def test_job_listing_easy_apply_defaults_false():
    from models import JobListing
    job = JobListing(url="https://au.seek.com/job/1", title="DevOps",
                     company="Acme", board="seek", description="AWS")
    assert job.easy_apply is False


def test_job_listing_easy_apply_can_be_set():
    from models import JobListing
    job = JobListing(url="https://au.seek.com/job/1", title="DevOps",
                     company="Acme", board="seek", description="AWS",
                     easy_apply=True)
    assert job.easy_apply is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
venv/bin/pytest tests/test_scraper.py::test_job_listing_easy_apply_defaults_false tests/test_scraper.py::test_job_listing_easy_apply_can_be_set -v
```

Expected: `FAILED` — `JobListing.__init__() got an unexpected keyword argument 'easy_apply'`

- [ ] **Step 3: Add field to `models.py`**

In `models.py`, change:

```python
@dataclass
class JobListing:
    url: str
    title: str
    company: str
    board: str          # 'linkedin' | 'seek' | 'indeed'
    description: str
    posted_at: str = ""
```

to:

```python
@dataclass
class JobListing:
    url: str
    title: str
    company: str
    board: str          # 'linkedin' | 'seek' | 'indeed'
    description: str
    posted_at: str = ""
    easy_apply: bool = False
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
venv/bin/pytest tests/test_scraper.py::test_job_listing_easy_apply_defaults_false tests/test_scraper.py::test_job_listing_easy_apply_can_be_set -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add models.py tests/test_scraper.py
git commit -m "feat: add easy_apply field to JobListing"
```

---

### Task 2: Detect Easy Apply in SeekScraper

**Files:**
- Modify: `scraper/seek.py`
- Test: `tests/test_scraper.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scraper.py`:

```python
@pytest.mark.asyncio
async def test_seek_scraper_sets_easy_apply_true(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from scraper.seek import SeekScraper
    s = SeekScraper()
    raw = [{"url": "https://au.seek.com/job/1", "title": "DevOps",
            "company": "Acme", "description": "AWS", "posted_at": "2026-04-01",
            "easy_apply": True}]
    result = s._filter_seen(raw)
    assert result[0]["easy_apply"] is True


@pytest.mark.asyncio
async def test_seek_scraper_sets_easy_apply_false(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from scraper.seek import SeekScraper
    s = SeekScraper()
    raw = [{"url": "https://au.seek.com/job/2", "title": "PM",
            "company": "Corp", "description": "Leadership", "posted_at": "2026-04-01",
            "easy_apply": False}]
    result = s._filter_seen(raw)
    assert result[0]["easy_apply"] is False
```

- [ ] **Step 2: Run to confirm they pass (no seek.py change needed — filter_seen passes dicts through)**

```bash
venv/bin/pytest tests/test_scraper.py::test_seek_scraper_sets_easy_apply_true tests/test_scraper.py::test_seek_scraper_sets_easy_apply_false -v
```

Expected: `PASSED` — `_filter_seen` already passes dicts through unchanged.

- [ ] **Step 3: Update `_scrape_skill` in `scraper/seek.py` to detect Easy Apply**

In `scraper/seek.py`, find the card parsing loop inside `_scrape_skill`. Change:

```python
                    href = await link.get_attribute("href")
                    url = f"https://au.seek.com{href}".split("?")[0]
                    title = (await link.inner_text()).strip()
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    posted = (await date_el.inner_text()).strip() if date_el else ""
                    desc = await self._fetch_description(url)
                    jobs.append({"url": url, "title": title, "company": company,
                                 "description": desc, "posted_at": posted})
```

to:

```python
                    href = await link.get_attribute("href")
                    url = f"https://au.seek.com{href}".split("?")[0]
                    title = (await link.inner_text()).strip()
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    posted = (await date_el.inner_text()).strip() if date_el else ""
                    card_text = (await card.inner_text()).lower()
                    easy_apply = "easy apply" in card_text
                    desc = await self._fetch_description(url)
                    jobs.append({"url": url, "title": title, "company": company,
                                 "description": desc, "posted_at": posted,
                                 "easy_apply": easy_apply})
```

- [ ] **Step 4: Update `scrape()` in `scraper/seek.py` to pass `easy_apply` to `JobListing`**

Find the `JobListing` construction at the bottom of `scrape()`. Change:

```python
        return [
            JobListing(url=j["url"], title=j["title"], company=j["company"],
                       board="seek", description=j["description"], posted_at=j.get("posted_at", ""))
            for j in new_jobs
        ]
```

to:

```python
        return [
            JobListing(url=j["url"], title=j["title"], company=j["company"],
                       board="seek", description=j["description"],
                       posted_at=j.get("posted_at", ""),
                       easy_apply=j.get("easy_apply", False))
            for j in new_jobs
        ]
```

- [ ] **Step 5: Run full scraper test suite**

```bash
venv/bin/pytest tests/test_scraper.py -v
```

Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add scraper/seek.py tests/test_scraper.py
git commit -m "feat: detect Easy Apply badge in SeekScraper"
```

---

### Task 3: Skip non-Easy-Apply jobs before tailoring

**Files:**
- Modify: `main.py`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write the failing test**

Read `tests/test_daemon.py` first to understand the existing pattern, then add:

```python
@pytest.mark.asyncio
async def test_application_worker_skips_non_easy_apply_seek_job(tmp_path, monkeypatch):
    import asyncio, tracker, main
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from models import JobListing

    job = JobListing(url="https://au.seek.com/job/99", title="PM",
                     company="Corp", board="seek", description="Lead",
                     easy_apply=False)

    upserted = []
    monkeypatch.setattr(tracker, "upsert_application", lambda app: upserted.append(app))

    score_calls = []
    async def fake_score(j):
        score_calls.append(j)
        return 80, "Great match"
    monkeypatch.setattr(main, "score_job", fake_score)

    cfg = {"candidate": {"name": "Sagar", "email": "s@e.com", "phone": "000"},
           "search": {"match_threshold": 45}}

    queue = asyncio.Queue()
    await queue.put(job)

    worker = asyncio.create_task(main.application_worker(queue, cfg))
    await queue.join()
    worker.cancel()

    assert len(upserted) == 1
    assert upserted[0].status == "skipped"
    assert "not Easy Apply" in upserted[0].notes
    # Score was still computed (job was evaluated before being skipped)
    assert len(score_calls) == 1
```

- [ ] **Step 2: Run to confirm it fails**

```bash
venv/bin/pytest tests/test_daemon.py::test_application_worker_skips_non_easy_apply_seek_job -v
```

Expected: `FAILED` — the job goes past the Easy Apply check (not implemented yet) and tries to tailor.

- [ ] **Step 3: Add Easy Apply check to `application_worker` in `main.py`**

Find this block in `application_worker`:

```python
            threshold = cfg["search"]["match_threshold"]
            if score < threshold:
                app.status = "skipped"
                app.notes = f"Score {score}% below threshold {threshold}%"
                logger.info(f"Skipped ({score}%): {job.title} @ {job.company}")
            else:
```

Change to:

```python
            threshold = cfg["search"]["match_threshold"]
            if score < threshold:
                app.status = "skipped"
                app.notes = f"Score {score}% below threshold {threshold}%"
                logger.info(f"Skipped ({score}%): {job.title} @ {job.company}")
            elif job.board == "seek" and not job.easy_apply:
                app.status = "skipped"
                app.notes = "External apply — not Easy Apply"
                logger.info(f"Skipped (external apply): {job.title} @ {job.company}")
            else:
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
venv/bin/pytest tests/test_daemon.py::test_application_worker_skips_non_easy_apply_seek_job -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full daemon test suite**

```bash
venv/bin/pytest tests/test_daemon.py -v
```

Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_daemon.py
git commit -m "feat: skip non-Easy-Apply Seek jobs before tailoring"
```

---

### Task 4: Lower match threshold to 45%

**Files:**
- Modify: `config.yaml`

- [ ] **Step 1: Update threshold in `config.yaml`**

Change:

```yaml
  match_threshold: 65
```

to:

```yaml
  match_threshold: 45
```

- [ ] **Step 2: Verify config loads correctly**

```bash
venv/bin/python -c "import yaml; c = yaml.safe_load(open('config.yaml')); print(c['search']['match_threshold'])"
```

Expected output: `45`

- [ ] **Step 3: Commit**

```bash
git add config.yaml
git commit -m "config: lower match threshold to 45%"
```

---

### Task 5: Expand Harvard ATS tailoring with 1-page constraint

**Files:**
- Modify: `tailorer.py`
- Test: `tests/test_tailorer.py`

- [ ] **Step 1: Write failing tests**

Replace the content of `tests/test_tailorer.py` with:

```python
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

COVER_RESPONSE = "Dear Hiring Manager,\n\nI am a great fit.\n\nSincerely,\nSagar"


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
        doc.add_paragraph("SAGAR VERMA").runs[0].bold = True
        doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
        doc.add_heading("WORK EXPERIENCE", level=1)
        doc.add_heading("Acme Corp", level=1)
        doc.add_paragraph("DevOps Engineer\t2020 - 2023")
        p = doc.add_paragraph("Maintained servers.", style="List Paragraph")
        p = doc.add_paragraph("Built pipelines.", style="List Paragraph")
        doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
        p = doc.add_paragraph("Skills: AWS, Docker")
        p.runs[0].bold = True
        doc.save(assets / "SAGAR VERMA.docx")
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)


@pytest.mark.asyncio
async def test_tailor_returns_two_pdf_paths():
    import tailorer
    mock_msg = MagicMock()
    mock_msg.content[0].text = EXPANDED_RESPONSE
    cover_msg = MagicMock()
    cover_msg.content[0].text = COVER_RESPONSE

    async def fake_lo(docx_path, out_dir):
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailorer._client.messages.create", new=AsyncMock(side_effect=[mock_msg, cover_msg])), \
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
    """The prompt sent to Claude must request summary, bullets, skills, experience_headline."""
    import tailorer
    captured = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        msg = MagicMock()
        msg.content[0].text = EXPANDED_RESPONSE
        return msg

    with patch("tailorer._client.messages.create", new=fake_create):
        await tailorer._tailor_resume_text(JOB)

    prompt = captured["messages"][0]["content"]
    assert "summary" in prompt
    assert "bullets" in prompt
    assert "skills" in prompt
    assert "experience_headline" in prompt


@pytest.mark.asyncio
async def test_tailor_resume_prompt_enforces_one_page_limits():
    """Prompt must enforce word/char limits to prevent PDF overflow."""
    import tailorer
    captured = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        msg = MagicMock()
        msg.content[0].text = EXPANDED_RESPONSE
        return msg

    with patch("tailorer._client.messages.create", new=fake_create):
        await tailorer._tailor_resume_text(JOB)

    prompt = captured["messages"][0]["content"]
    assert "60 words" in prompt or "60-word" in prompt
    assert "20 words" in prompt or "20-word" in prompt
    assert "8 bullets" in prompt


@pytest.mark.asyncio
async def test_export_resume_pdf_patches_skills_line(tmp_path, monkeypatch):
    """Skills paragraph (bold Normal starting with 'Skills:') is replaced."""
    from docx import Document
    import tailorer
    from pathlib import Path

    assets = tmp_path / "assets"
    assets.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)
    monkeypatch.setattr("tailorer._libreoffice_path", "/fake/soffice")

    doc = Document()
    doc.add_paragraph("SAGAR VERMA").runs[0].bold = True
    doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
    doc.add_heading("WORK EXPERIENCE", level=1)
    doc.add_heading("Acme Corp", level=1)
    doc.add_paragraph("DevOps Engineer\t2020 - 2023")
    doc.add_paragraph("Old bullet.", style="List Paragraph")
    doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
    p = doc.add_paragraph("Skills: OldSkill1, OldSkill2")
    p.runs[0].bold = True
    doc.save(assets / "SAGAR VERMA.docx")

    sections = {
        "summary": "Experienced platform engineer.",
        "bullets": ["New bullet one.", "New bullet two."],
        "skills": "AWS, Terraform, Kubernetes",
        "experience_headline": "Platform Engineer",
    }

    saved_docx = None

    async def fake_lo(docx_path, out_dir):
        nonlocal saved_docx
        saved_docx = docx_path
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailorer._run_libreoffice", new=fake_lo):
        await tailorer._export_resume_pdf(sections, JOB)

    assert saved_docx is not None
    result_doc = Document(saved_docx.replace(".pdf", ".docx") if saved_docx.endswith(".pdf") else saved_docx)

    # Find skills paragraph
    skills_paras = [p.text for p in result_doc.paragraphs if p.text.startswith("Skills:")]
    # It should now contain our new skills
    assert any("AWS" in t and "Terraform" in t for t in skills_paras)


@pytest.mark.asyncio
async def test_export_resume_pdf_patches_experience_headline(tmp_path, monkeypatch):
    """Experience headline (Normal paragraph with tab + year) is updated."""
    from docx import Document
    import tailorer
    from pathlib import Path

    assets = tmp_path / "assets"
    assets.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr("tailorer.ASSETS_DIR", assets)
    monkeypatch.setattr("tailorer.OUTPUT_DIR", output)
    monkeypatch.setattr("tailorer._libreoffice_path", "/fake/soffice")

    doc = Document()
    doc.add_paragraph("SAGAR VERMA").runs[0].bold = True
    doc.add_paragraph("sagarverma1997@gmail.com | +61 491 621 148")
    doc.add_heading("WORK EXPERIENCE", level=1)
    doc.add_heading("Acme Corp", level=1)
    doc.add_paragraph("DevOps Engineer\t2020 - 2023")
    doc.add_paragraph("Did stuff.", style="List Paragraph")
    doc.add_heading("SKILLS & CERTIFICATIONS", level=1)
    p = doc.add_paragraph("Skills: AWS")
    p.runs[0].bold = True
    doc.save(assets / "SAGAR VERMA.docx")

    sections = {
        "summary": "Experienced engineer.",
        "bullets": ["New bullet."],
        "skills": "AWS, Terraform",
        "experience_headline": "Platform & Automation Engineer",
    }

    saved_docx = None

    async def fake_lo(docx_path, out_dir):
        nonlocal saved_docx
        saved_docx = docx_path
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("tailorer._run_libreoffice", new=fake_lo):
        await tailorer._export_resume_pdf(sections, JOB)

    result_doc = Document(saved_docx)
    headlines = [p.text for p in result_doc.paragraphs if "\t" in p.text and any(c.isdigit() for c in p.text)]
    assert any("Platform & Automation Engineer" in h for h in headlines)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
venv/bin/pytest tests/test_tailorer.py -v
```

Expected: `test_tailor_resume_prompt_requests_all_four_sections` and `test_tailor_resume_prompt_enforces_one_page_limits` FAIL (prompt doesn't include the new fields yet); DOCX patching tests FAIL.

- [ ] **Step 3: Rewrite `_tailor_resume_text` in `tailorer.py`**

Replace the `_tailor_resume_text` function:

```python
async def _tailor_resume_text(job: JobListing) -> dict:
    profile = (ASSETS_DIR / "profile.txt").read_text()
    model = os.environ.get("WRITER_MODEL", "claude-sonnet-4-6")

    async def _call():
        msg = await _client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Rewrite these resume sections to best match the job, following Harvard ATS guidelines.\n\n"
                    f"JOB TITLE: {job.title}\nCOMPANY: {job.company}\n\n"
                    f"JOB DESCRIPTION:\n{job.description[:3000]}\n\n"
                    f"CURRENT RESUME:\n{profile}\n\n"
                    "RULES (strictly follow for ATS and 1-page fit):\n"
                    "- summary: 2 sentences, max 60 words. Start with the job title from the JD. "
                    "Include 2-3 exact hard-skill keywords from the JD. Strong action verbs.\n"
                    "- bullets: Exactly 8 bullets, each max 20 words. Format: "
                    "[Past-tense action verb] + [specific achievement] + [quantified metric]. "
                    "Mirror exact keywords/acronyms from the JD verbatim.\n"
                    "- skills: Single compact comma-separated string, max 200 characters. "
                    "Order skills by relevance to JD. Use exact terminology from JD.\n"
                    "- experience_headline: Max 6 words. Job title mirroring JD role terminology.\n\n"
                    'Return JSON only — no markdown fences:\n'
                    '{"summary": "...", "bullets": ["...", ...], '
                    '"skills": "...", "experience_headline": "..."}'
                ),
            }],
        )
        try:
            text = msg.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise ValueError(f"Unexpected resume API response: {msg.content[0].text!r}") from exc

    return await retry_async(_call)
```

- [ ] **Step 4: Rewrite `_export_resume_pdf` in `tailorer.py`**

Replace the entire `_export_resume_pdf` function:

```python
async def _export_resume_pdf(sections: dict, job: JobListing) -> str:
    filename = make_filename("SagarVerma", job.company, job.title)
    doc = Document(ASSETS_DIR / "SAGAR VERMA.docx")

    summary = sections.get("summary", "")
    bullets = sections.get("bullets", [])
    skills_text = sections.get("skills", "")
    headline = sections.get("experience_headline", "")

    # --- Insert summary paragraph after the contact info line ---
    # Contact line is identified by having '@' in it (email address).
    contact_para = None
    for para in doc.paragraphs:
        if "@" in para.text:
            contact_para = para
            break

    if contact_para and summary:
        # Insert a new paragraph directly after the contact paragraph using XML
        from docx.oxml import OxmlElement
        new_p = OxmlElement("w:p")
        contact_para._element.addnext(new_p)
        from docx.text.paragraph import Paragraph as DocxParagraph
        summary_para = DocxParagraph(new_p, contact_para._element.getparent())
        summary_para.style = doc.styles["Normal"]
        summary_para.add_run(summary)

    # --- Patch remaining sections ---
    in_work_experience = False
    bullets_applied = 0

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        # Track current section via Heading 1
        if para.style.name == "Heading 1":
            section_lower = text.lower()
            in_work_experience = "work experience" in section_lower or "experience" in section_lower
            continue

        # Skip name paragraph (bold Normal, no '@', no tab+year)
        if para.style.name == "Normal" and any(r.bold for r in para.runs) and "@" not in text:
            # Skills line starts with "Skills:"
            if text.startswith("Skills:") and skills_text:
                for run in para.runs:
                    run.text = ""
                if para.runs:
                    para.runs[0].text = f"Skills: {skills_text}"
            continue

        # Experience headline: Normal style, contains tab, contains a 4-digit year
        if (para.style.name == "Normal" and "\t" in text
                and any(part.strip().isdigit() or (len(part.strip()) == 4 and part.strip().isdigit())
                        for part in text.replace("-", " ").split())
                and headline and in_work_experience):
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
```

- [ ] **Step 5: Run the tailorer tests**

```bash
venv/bin/pytest tests/test_tailorer.py -v
```

Expected: all `PASSED`

- [ ] **Step 6: Run the full test suite**

```bash
venv/bin/pytest tests/ -v
```

Expected: all `PASSED`

- [ ] **Step 7: Commit**

```bash
git add tailorer.py tests/test_tailorer.py
git commit -m "feat: Harvard ATS tailoring — summary, bullets, skills, headline with 1-page limits"
```

---

### Task 6: Fix `setup_sessions.py` to strip unsupported cookie fields on save

**Files:**
- Modify: `setup_sessions.py`

This prevents the `partitionKey: expected string, got object` Playwright error that appeared during testing whenever a saved session is reloaded.

- [ ] **Step 1: Add cookie cleanup to `setup_board` in `setup_sessions.py`**

Find the session save block in `setup_board`:

```python
    # Save session
    await context.storage_state(path=str(state_file))
    print(f"  Session saved to {state_file}")
```

Change to:

```python
    # Save session
    await context.storage_state(path=str(state_file))

    # Strip Chrome-only cookie fields that Playwright rejects on reload
    import json
    raw = json.loads(state_file.read_text())
    for cookie in raw.get("cookies", []):
        for field in ("partitionKey", "priority", "sourceScheme", "sourcePort", "session", "size"):
            cookie.pop(field, None)
    state_file.write_text(json.dumps(raw))
    print(f"  Session saved to {state_file}")
```

- [ ] **Step 2: Verify the fix applies cleanly to the existing session**

```bash
venv/bin/python -c "
import json
from pathlib import Path
data = json.loads(Path('sessions/seek/state.json').read_text())
bad = [i for i, c in enumerate(data['cookies'])
       if any(isinstance(c.get(f), dict) for f in ('partitionKey',))]
print('Bad partitionKey cookies:', bad)
print('All clean' if not bad else 'STILL HAS ISSUES')
"
```

Expected: `All clean`

- [ ] **Step 3: Commit**

```bash
git add setup_sessions.py
git commit -m "fix: strip unsupported Chrome cookie fields from saved sessions"
```

---

## Self-Review

**Spec coverage:**
- ✅ Easy Apply field on JobListing (Task 1)
- ✅ Detect Easy Apply in scraper (Task 2)
- ✅ Skip non-Easy-Apply jobs before tailoring, track as skipped (Task 3)
- ✅ Threshold lowered to 45% (Task 4)
- ✅ Harvard ATS prompt with summary/bullets/skills/headline (Task 5)
- ✅ 1-page constraint enforced in prompt (60 words summary, 8 bullets × 20 words, 200 char skills) (Task 5)
- ✅ Bug fix: name paragraph no longer overwritten as summary (Task 5)
- ✅ Session cookie fix (Task 6)

**Placeholder scan:** No TBDs or incomplete steps found.

**Type consistency:**
- `easy_apply: bool = False` defined in Task 1, used in Tasks 2, 3 ✅
- `sections` dict keys: `summary`, `bullets`, `skills`, `experience_headline` — consistent across prompt (Task 5 Step 3) and DOCX patching (Task 5 Step 4) ✅
- `_tailor_resume_text` return type is `dict` — consumed by `_export_resume_pdf(sections: dict, ...)` ✅
