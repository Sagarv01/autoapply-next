# Easy Apply + Harvard ATS Tailoring — Design Spec

**Date:** 2026-04-02

---

## Goal

Filter Seek applications to Easy Apply jobs only (avoiding unpredictable external ATS systems), lower the match threshold to 45%, and expand the resume/cover letter tailoring to rewrite more sections using Harvard ATS criteria — while keeping the resume to exactly one page.

---

## Change 1: Easy Apply Filtering

### Detection
During `SeekScraper._scrape_skill()`, after extracting each card's title/company/URL, check whether the card contains "Easy Apply" text (case-insensitive). This is robust to Seek's DOM changes:

```python
card_text = (await card.inner_text()).lower()
easy_apply = "easy apply" in card_text
```

Store as `easy_apply: bool` in the job dict and pass through to `JobListing`.

### Model change
`models.py`: add `easy_apply: bool = False` to `JobListing`.

### Filtering in worker
In `application_worker` (main.py), after scoring, add:

```python
elif job.board == "seek" and not job.easy_apply:
    app.status = "skipped"
    app.notes = "External apply — not Easy Apply"
```

Non-Easy-Apply jobs are still scored, still written to the tracker/Excel as `skipped`, and `queue.task_done()` is still called. No wasted tailor API calls.

---

## Change 2: Match Threshold

`config.yaml`: `match_threshold: 45`

---

## Change 3: Harvard ATS Tailoring

### Expanded Claude response schema

```json
{
  "summary": "2 sentences max, 60 words max. Starts with job title from JD. Uses exact keywords from JD verbatim. Strong action verbs.",
  "bullets": ["8 bullets, each max 20 words. Action verb → achievement → quantified metric. Mirror exact JD keywords."],
  "skills": "Compact comma-separated skills string, max 200 chars. Keywords in same order/terminology as JD.",
  "experience_headline": "6 words max. Job title mirroring JD terminology."
}
```

### Harvard ATS rules baked into the prompt
- Every bullet: action verb (past tense) → specific achievement → quantified result (%, $, #)
- Mirror exact JD keywords/acronyms verbatim (ATS scanners match exact strings)
- Summary includes job title from JD and 2–3 hard skills from JD
- Skills ordered by relevance to JD, using JD's exact terminology
- No tables, no columns, standard section headings (preserved from DOCX template)

### DOCX patching — sections rewritten

| Section | Identification | Replacement |
|---------|---------------|-------------|
| Summary | Insert new paragraph after contact line (has `@`) | `summary` field |
| Bullets | `style='List Paragraph'`, in WORK EXPERIENCE | `bullets` list |
| Skills line | `style='Normal'`, bold, starts with `"Skills:"` | `skills` field |
| Experience headline | `style='Normal'`, not bold, contains `\t` + 4-digit year | replace text before `\t` with `experience_headline` |

### Bug fix
Current tailorer wrongly replaces paragraph `[00]` ("SAGAR VERMA") as the summary. This is fixed by the new logic above — name paragraph is explicitly skipped (bold Normal paragraph at index 0).

### 1-page constraint
- Summary: exactly 2 sentences, hard cap 60 words (enforced in prompt)
- Bullets: exactly 8 (same count as current Thinklayer bullets), each max 20 words
- Skills: max 200 characters
- Experience headline: max 6 words
- Inserting a summary paragraph adds ~3 lines; offset by the fact that the old (wrong) summary replacement is removed

---

## Files Changed

| File | Change |
|------|--------|
| `models.py` | Add `easy_apply: bool = False` to `JobListing` |
| `scraper/seek.py` | Detect Easy Apply in `_scrape_skill()`, pass flag through |
| `main.py` | Skip non-Easy-Apply seek jobs before tailoring |
| `config.yaml` | `match_threshold: 45` |
| `tailorer.py` | Expanded prompt + DOCX patching for summary/bullets/skills/headline |
| `tests/test_scraper.py` | Test Easy Apply detection |
| `tests/test_tailorer.py` | Test expanded section patching |
