# Job Bot — Design Spec
**Date:** 2026-04-01  
**Status:** Approved  

---

## Overview

An autonomous Python daemon that runs 24/7, scraping LinkedIn, Seek, and Indeed for jobs in Australia, evaluating each against Sagar Verma's profile using Claude AI, and automatically applying with a tailored resume and cover letter when the match score is ≥ 65%. Priority is speed — the bot must apply before other candidates.

---

## Legal / Risk Notice

Automated scraping and application submission on LinkedIn, Seek, and Indeed violates each platform's Terms of Service. LinkedIn prohibits automated data collection; Seek's Terms explicitly ban bots and scrapers; Indeed similarly prohibits automated access. Running this bot risks account suspension or termination on all three platforms. The operator accepts this risk.

---

## Architecture

### Hybrid approach: Raw Playwright for scraping + browser-use for applying

- **Scraping** — Raw Playwright with stealth patching runs lightweight parallel scrapers across all three job boards simultaneously. No AI overhead during discovery — just fast DOM scraping to extract job titles, descriptions, URLs, and company names. Persistent browser sessions (cookie store reused across cycles) avoid repeated logins which are a strong bot signal.
- **Matching** — Claude Haiku scores each job description against Sagar's profile (0–100). Jobs below 65% are discarded immediately.
- **Tailoring** — Claude Sonnet rewrites the base `SAGAR VERMA.docx` to align with the job, exports a tailored PDF resume and a separate cover letter PDF.
- **Applying** — A `browser-use` agent driven by Claude Sonnet navigates to the job URL and submits the application intelligently. No hard-coded form selectors — handles any layout, multi-step flows, and detects cover letter upload fields automatically.
- **Tracking** — Every outcome (applied, skipped, failed) is logged to SQLite (for deduplication) and Excel (for human review).

### Continuous loop

Scrapers run every **5 minutes** (± 60 seconds randomised) across all three boards in parallel via `asyncio.gather`. The moment a new job is found, it is placed on an `asyncio.Queue`. A single applicator worker consumes the queue serially — one application at a time — preventing uncontrolled concurrent `browser-use` agents.

---

## Components

### 1. Scraper (`scraper/`)
- `__init__.py`, `linkedin.py`, `seek.py`, `indeed.py`
- Each scraper runs as an async Playwright task using `playwright-stealth` to suppress `navigator.webdriver` and CDP detection signals
- Sessions are persistent: browser profile/cookie store saved to `sessions/linkedin/`, `sessions/seek/`, `sessions/indeed/`. Login only occurs when cookies are expired or invalid — not on every cycle.
- **Searches by skills, not job titles** — multiple keyword searches run per cycle, one per core skill. This catches roles with non-standard titles (e.g. "AI Conversation Engineer", "CX Automation Specialist", "Contact Centre Architect") that title-based search would miss.
- Search terms (each run as a separate query, results de-duplicated by URL):
  - `Amazon Connect`
  - `Terraform AWS`
  - `DevOps AWS`
  - `Kubernetes AWS`
  - `Amazon Lex`
  - `Conversational AI`
  - `IVR contact centre`
  - `CI/CD AWS`
- Location filter: Australia only
- Extracts: job title, company, URL, full description, date posted
- Filters out any job URL already in the SQLite `seen_jobs` table
- Yields new jobs to the `asyncio.Queue` immediately

### 2. Matcher (`matcher.py`)
- Model: **`gpt-4o-mini`** via OpenAI API (fast, cheap — just scoring. Candidate profile cached via OpenAI prompt caching to cut repeated token cost)
- Input: full job description + Sagar's full profile text (loaded from `assets/profile.txt` — the complete resume content, not just a short summary)
- Output: JSON with `score` (0–100) and `reasoning` (one sentence)
- Threshold: ≥ 65% proceeds to tailoring; below 65% logged to SQLite as `skipped`
- Rate limit handling: exponential backoff on HTTP 429 (1s → 2s → 4s, max 3 retries), then skip and log

### 3. Tailorer (`tailorer.py`)
- Model: **`claude-sonnet-4-6`** for resume tailoring and cover letter generation
- Resume: loads `assets/SAGAR VERMA.docx`, rewrites the summary section and top bullet points using `python-docx`, exports to PDF via LibreOffice headless
- Cover letter: generates a one-page cover letter, rendered to PDF via `reportlab`
- Both files saved to `output/` with unique names (see File Naming)
- PDF conversion is queued serially through an `asyncio.Lock` to prevent LibreOffice concurrent-write race conditions (LibreOffice is not thread-safe)
- LibreOffice binary path: macOS → `/Applications/LibreOffice.app/Contents/MacOS/soffice`; Linux → `libreoffice`. Detected at startup, error raised if not found.
- If LibreOffice conversion fails (non-zero exit or output PDF not created): log error, mark application as Failed, skip — do not proceed to apply step.
- Rate limit handling: same exponential backoff as Matcher

### 4. Applicator (`applicator.py`)
- Runs as a single async worker consuming from the `asyncio.Queue` — strictly serial, one application at a time
- Uses `browser-use` `Agent` with model `claude-haiku-4-5-20251001` (`ChatAnthropic`)
- Task prompt: navigate to job URL, fill application form with candidate details (name, email, phone from `.env`), upload tailored resume PDF, upload cover letter if field exists, submit
- Human-like delays: random 1–4 second pauses between actions via browser-use agent custom instructions
- On CAPTCHA or rate limit: raises `BoardBlockedError`, caught by daemon which pauses that board's scraper for 15 minutes
- On agent timeout (> 5 minutes): log as Failed, save screenshot, continue queue

### 5. Tracker (`tracker.py`)
- **SQLite** (`jobs.db`): WAL journal mode enabled to handle concurrent async reads/writes safely
  - `seen_jobs` table: URL + timestamp (deduplication)
  - `applications` table: full record per job with status (`skipped`, `in_progress`, `applied`, `failed`)
  - `in_progress` status written before apply starts; updated to `applied` or `failed` on completion — allows orphaned application detection on daemon restart
- **Excel** (`output/JobApplications_2026.xlsx`): uses `openpyxl` with a file-level `threading.Lock` to prevent concurrent write corruption. New row appended per application. File transitions to `JobApplications_2027.xlsx` on year rollover.
- On startup: any jobs left in `in_progress` state from a previous run are logged as `failed` and re-queued for retry.

### 6. Daemon (`main.py`)
- Async event loop — all three scrapers run concurrently via `asyncio.gather`
- Single `asyncio.Queue` feeds the serial applicator worker
- Per-board error isolation: if one board's scraper fails, the other two continue
- Email alert via Gmail SMTP if credentials are rejected on login
- Logging: `RotatingFileHandler` — max 10 MB per file, 5 backups — written to `bot.log`
- On startup: initialises `output/`, `errors/`, `sessions/` directories if they don't exist; detects LibreOffice binary path; validates `.env` keys present

---

## File Naming

Timestamp includes milliseconds to prevent collisions under rapid processing.

**Resume:**  
`SagarVerma_[CompanyName]_[JobTitle]_[YYYYMMDD_HHMMSS_mmm].pdf`  
Example: `SagarVerma_Atlassian_DevOpsEngineer_20260401_143211_042.pdf`

**Cover letter:**  
`CoverLetter_[CompanyName]_[JobTitle]_[YYYYMMDD_HHMMSS_mmm].pdf`  
Example: `CoverLetter_Atlassian_DevOpsEngineer_20260401_143211_042.pdf`

Company and job title are sanitised: spaces → CamelCase, all special characters removed.

---

## Excel Tracker Schema

| Column | Example |
|--------|---------|
| Timestamp | 2026-04-01 14:32:11 |
| Job Title | DevOps Engineer |
| Company | Atlassian |
| Board | LinkedIn |
| Job URL | linkedin.com/jobs/view/123456 |
| Match Score | 78% |
| Resume File | SagarVerma_Atlassian_DevOpsEngineer_20260401_143211_042.pdf |
| Cover Letter | CoverLetter_Atlassian_DevOpsEngineer_20260401_143211_042.pdf |
| Status | Applied / Skipped / Failed |
| Notes | Cover letter uploaded / CAPTCHA blocked / PDF conversion failed |

File: `output/JobApplications_2026.xlsx` — one sheet, row appended per event, protected by file-level lock.

---

## AI Models

| Task | Provider | Model ID | Daily Cost (est.) | Reason |
|------|----------|----------|-------------------|--------|
| Job matching | OpenAI | `gpt-4o-mini` | ~$0.04 | Just scoring — fast, cheap, structured output. Prompt caching cuts cost further. |
| Resume tailoring | Anthropic | `claude-sonnet-4-6` | ~$0.90 | Goes directly to recruiters — quality drives callback rate |
| Cover letter | Anthropic | `claude-sonnet-4-6` | ~$0.68 | Same reasoning as resume tailoring |
| Browser automation | Anthropic | `claude-haiku-4-5-20251001` | ~$1.38 | As capable as prior-gen Sonnet for tool use; agentic sessions burn tokens fast |
| **Total** | | | **~$3/day · ~$90/month** | |

Model IDs are configurable via `.env` (`MATCHER_MODEL`, `WRITER_MODEL`, `AGENT_MODEL`) so they can be swapped without code changes.

---

## Error Handling

| Scenario | Response |
|----------|----------|
| Board blocks session (CAPTCHA / rate limit) | Pause that board's scraper 15 min, continue others |
| Application fails mid-way | Save screenshot to `errors/`, log as Failed |
| Claude API rate limit (429) | Exponential backoff: 1s → 2s → 4s, max 3 retries, then skip and log |
| Claude API error (non-429) | Retry once immediately, then skip and log |
| Login credentials rejected | Send Gmail SMTP alert immediately |
| `browser-use` agent times out (> 5 min) | Log as Failed, save screenshot, continue queue |
| LibreOffice conversion fails | Log as Failed, skip apply step, continue queue |
| `output/` / `errors/` / `sessions/` not found | Created automatically on daemon startup |
| Excel write conflict | Serialised via `threading.Lock` — no conflict possible |
| SQLite lock contention | WAL mode enabled — concurrent reads/writes safe |
| Daemon restart with orphaned `in_progress` jobs | Detected on startup, re-queued for retry |

---

## Anti-Detection (Phase 1)

- `playwright-stealth` applied to all scraper browser contexts (suppresses `navigator.webdriver`, CDP signals)
- Persistent session cookies per board — login only when session expires
- Random 1–4 second delays between every browser action (scraping and applying)
- Scrape intervals randomised ± 60 seconds around 5-minute target
- User-agent rotated per session

Proxy rotation deferred to Phase 2 if accounts get flagged.

---

## Credentials & Security

All secrets are stored in a **`.env` file** (never committed to version control). `config.yaml` contains only non-secret settings.

`.env` keys:
```
ANTHROPIC_API_KEY=
LINKEDIN_EMAIL=
LINKEDIN_PASSWORD=
SEEK_EMAIL=
SEEK_PASSWORD=
INDEED_EMAIL=
INDEED_PASSWORD=
GMAIL_USER=
GMAIL_APP_PASSWORD=
ALERT_EMAIL=
OPENAI_API_KEY=
MATCHER_MODEL=gpt-4o-mini
WRITER_MODEL=claude-sonnet-4-6
AGENT_MODEL=claude-haiku-4-5-20251001
```

`.gitignore` must include: `.env`, `jobs.db`, `output/`, `errors/`, `sessions/`, `bot.log*`, `venv/`

`config.yaml` is non-secret and should be committed. A `config.yaml.example` should also be provided as a reference template.

---

## Configuration (`config.yaml`)

```yaml
candidate:
  name: "Sagar Verma"
  email: "sagarverma1997@gmail.com"
  phone: "+61XXXXXXXXX"

search:
  skills:
    - "Amazon Connect"
    - "Terraform AWS"
    - "DevOps AWS"
    - "Kubernetes AWS"
    - "Amazon Lex"
    - "Conversational AI"
    - "IVR contact centre"
    - "CI/CD AWS"
  location: "Australia"
  match_threshold: 65

scraper:
  interval_seconds: 300
  interval_jitter_seconds: 60
  board_block_pause_minutes: 15
```

---

## System Requirements

- Python 3.11+
- **LibreOffice** (for DOCX → PDF conversion)
  - macOS: `brew install --cask libreoffice`
  - Linux: `sudo apt install libreoffice`
- Chromium (installed via `playwright install chromium`)

---

## `requirements.txt`

```
anthropic
openai
browser-use
playwright
playwright-stealth
python-docx
reportlab
openpyxl
python-dotenv
aiofiles
```

---

## Project Structure

```
job-finder/
├── .env                         # secrets (gitignored)
├── .gitignore
├── config.yaml                  # non-secret settings
├── main.py                      # daemon entry point
├── matcher.py                   # Claude Haiku scoring
├── tailorer.py                  # Claude Sonnet resume + cover letter
├── applicator.py                # browser-use agent (serial worker)
├── tracker.py                   # Excel + SQLite logging
├── scraper/
│   ├── __init__.py
│   ├── linkedin.py
│   ├── seek.py
│   └── indeed.py
├── assets/
│   ├── SAGAR VERMA.docx         # base resume
│   └── profile.txt              # full profile text for matcher
├── sessions/                    # persistent browser cookies (gitignored)
│   ├── linkedin/
│   ├── seek/
│   └── indeed/
├── output/                      # tailored resumes, cover letters, Excel tracker
├── errors/                      # screenshots of failed applications
├── jobs.db                      # SQLite (gitignored)
├── bot.log                      # rotating log (gitignored)
├── requirements.txt
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-04-01-job-bot-design.md
```

---

## Out of Scope

- Proxy rotation (Phase 2 if accounts get flagged)
- Web dashboard / UI
- Support for job boards outside Australia
- Responding to recruiter messages
