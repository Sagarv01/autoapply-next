# Job Finder Bot

An autonomous job application bot that scrapes Seek.com.au, scores jobs with AI, tailors your resume and cover letter, and applies via Seek Quick Apply — all on autopilot.

## What it does

1. **Scrapes** Seek.com.au for jobs matching your configured skills (all pages, sorted by date)
2. **Scores** each job using OpenAI GPT to assess fit
3. **Tailors** your resume and cover letter using Claude (Anthropic)
4. **Applies** via Seek Quick Apply using Playwright browser automation
5. **Tracks** everything in SQLite (`jobs.db`) and Excel (`output/JobApplications_YYYY.xlsx`)

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/Sagarv01/job-finder-bot.git
cd job-finder-bot
python -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |
| `OPENAI_API_KEY` | From [platform.openai.com](https://platform.openai.com) |
| `SEEK_EMAIL` | Your Seek account email |
| `ALERT_EMAIL` | Where to send failure alerts |

### 3. Configure search settings

Copy `config.yaml.example` to `config.yaml` and edit:

```bash
cp config.yaml.example config.yaml
```

- `candidate` — your name, email, phone
- `search.skills` — keywords to search on Seek
- `search.location` — e.g. `Australia`
- `scraper.interval_seconds` — how long to sleep between scrape cycles (default 5 min)

### 4. Set up your Seek session

```bash
venv/bin/python setup_sessions.py
```

This opens a browser for you to log in to Seek manually. The session is saved locally so the bot can apply without re-logging in.

### 5. Add your resume

Place your resume in `assets/` (`.docx` or `.pdf`). Update `assets/profile.txt` with a plain-text summary of your experience used for tailoring.

## Running

```bash
venv/bin/python main.py
```

The bot runs in a continuous loop:
1. Recovers any in-progress applications from a previous run
2. Scrapes all Seek pages for your skills
3. Applies to every Quick Apply job found
4. Sleeps, then repeats

Logs are written to `bot.log`.

## Project structure

```
main.py           — Entry point, main loop
scraper/          — Seek (and LinkedIn) scrapers
seek_apply.py     — Seek Quick Apply automation
applicator.py     — Orchestrates tailoring + applying per job
tailorer.py       — Resume/cover letter tailoring via Claude
matcher.py        — Job scoring via OpenAI
tracker.py        — SQLite + Excel tracking
models.py         — Shared data models
```

## Notes

- `.env`, `jobs.db`, `sessions/`, and `output/` are gitignored — your credentials and data stay local
- If the bot is interrupted mid-run, orphaned in-progress jobs are automatically re-queued on the next start
