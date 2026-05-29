# Fix stuck-question failures and runaway retries

## Problem (evidence-based)

Across 146 events in `errors/stuck_questions.jsonl`, four root causes are
producing every failed application:

### RC-1. Validation-error scanner grabs the wrong question heading
`_find_validation_errors` walks 10 ancestors up looking for any `legend, h2,
h3, label`. On Seek's nested cards it picks an outer card's heading instead
of the question the validation message belongs to.

Concrete example (`job/91805917`):
```
question (captured): "Visa Status"
error (captured):    "Notice Period - Please make a selection"
```
The bot then asks Claude "Visa Status?" with notice-period options. Claude
returns "485 Temporary Graduate Visa", which matches none of `["1 week", "2
weeks", ...]`, so the fallback picks `options[0]` = "1 week". The actual
"Visa Status" radio (which IS already correct on the page) gets nothing
done. Continue still fails. Loop repeats.

### RC-2. Citizenship Yes/No questions aren't hard-ruled
"Are you an Australian Citizen, with a minimum NV1 Security Clearance?" has
no "485" / "no restriction" / "temporary graduate" in its options (just
Yes/No). The current `_answer_radio_group` logic detects it as
`is_citizenship_q` and explicitly does NOT pick "Yes" — but then falls back
to Claude. Claude either fails or returns something that doesn't substring-
match "Yes"/"No", so `best_idx = 0` ticks "Yes" by default.

User facts:
- Australian citizen: **No**
- 485 Temporary Graduate visa (also called: "Temporary Graduate Visa",
  "Temporary visa with no restrictions", "Visa with no restrictions",
  "Subclass 485")
- Legally entitled to work in Australia: **Yes**
- Security clearances: **None** (no NV1, NV2, baseline, etc.)

### RC-3. Failed jobs get retried every scrape cycle
`tracker.is_applied_or_skipped` filters only `applied` / `skipped` from the
scraper. Anything `failed` is rescraped, rescored (wasted Claude API),
requeued (`INSERT OR IGNORE` so status stays `failed`), and `_apply_with_retry`
runs 3 fresh attempts. With ~30-min scrape cycles, one permanently-broken
job logs 3 stuck events × ~14 cycles = 42 events in 10 hours.

### RC-4. No structured per-application log
`bot.log` is text only; `errors/stuck_questions.jsonl` only fires when the
bot bails at the recovery cap. There's no per-job record of "what did
Claude answer for which question, did it stick, what was the page state."
Without it, we can't figure out *why* a fix didn't work after the fact.

---

## Fix plan (smallest changes that solve each RC)

### Step 1 — `seek_apply.py`: rewrite `_find_validation_errors` to use the fieldset

Stop walking 10 ancestors. Each Seek question lives in its own `<fieldset>`
with a `<legend>`. The validation-error element is inside the same
fieldset. Use `closest('fieldset')` and read its `<legend>` directly.
Fallback: if no fieldset (single-input questions), use the closest element
matching `[role=group], [class*="FormControl"]`.

```js
errorEls.forEach(err => {
    const text = (err.innerText || '').trim();
    if (!text) return;
    const fs = err.closest('fieldset, [role=group]');
    if (!fs) return;
    const legend = fs.querySelector('legend, [class*="legend"]');
    const input  = fs.querySelector('input,select,textarea');
    if (!legend || !input) return;
    const fid = input.id || input.name || '';
    if (seen.has(fid)) return; seen.add(fid);
    out.push({
        question: legend.innerText.trim().slice(0, 200),
        error:    text.slice(0, 250),
        field_id: fid,
        field_kind: input.tagName.toLowerCase() + (input.type ? ':' + input.type : ''),
    });
});
```

### Step 2 — `seek_apply.py`: hard-rule the four candidate-fact questions

In `_answer_radio_group`, *before* the 485-detection block, add explicit
hard rules. Match on the **question label**, then pick by option text:

```python
ql = question_label  # already lowercased

# Candidate facts (deterministic — never call Claude for these):
# - NOT an Australian citizen / PR
# - HAS work rights (485 visa)
# - NO security clearance of any level
HARD_RULES = [
    # (label-keywords, option-needle-to-pick)
    (("australian citizen", "permanent resident", "are you a citizen",
      "citizen of australia", "citizenship status"),         "no"),
    (("legally entitled to work", "right to work", "eligible to work",
      "authoris", "work in australia"),                      "yes"),
    (("security clearance", "nv1", "nv2", "baseline clearance",
      "negative vetting", "afp clearance", "agsva"),         "no"),
    (("notice period",),                                      "2 week"),
    (("salary expect", "expected salary"),                   "negotiab"),
]
for needles, pick in HARD_RULES:
    if any(n in ql for n in needles):
        idx = next((i for i, o in enumerate(options_lower) if pick in o), None)
        if idx is not None:
            await _check_radio(page, group["options"][idx])
            logger.info(f"    Radio (hard-rule '{pick}') for '{ql[:60]}'")
            return
        # Fall through to Claude only if pick option missing
        break
```

Edge case: the citizenship rule must run *before* the work-rights "yes"
rule, since "Are you an Australian citizen with right to work?" matches
both. Ordering above puts citizenship first.

### Step 3 — `seek_apply.py`: expand 485 visa aliases

Replace the current narrow check with a single sweep:

```python
VISA_485_ALIASES = (
    "485", "subclass 485",
    "temporary graduate", "graduate visa",
    "temporary visa with no restriction", "visa with no restriction",
    "no work restriction", "no restrictions",
    "post-study work", "post study work",
)
if any(any(a in o for a in VISA_485_ALIASES) for o in options_lower):
    best_idx = next(i for i, o in enumerate(options_lower)
                    if any(a in o for a in VISA_485_ALIASES))
```

Also update `_claude_answer`'s prompt: list these aliases so Claude returns
text the matcher will recognize.

### Step 4 — `seek_apply.py`: hard-rule errors in `_resolve_validation_errors` too

`_resolve_validation_errors` is the second-chance path after the form-step
handler. It currently goes straight to Claude. Apply the same `HARD_RULES`
table here on the (now-correct) question text before falling back to
Claude. Same logic, same constants — extract to module-level helper
`_hard_rule_answer(question_label, options) -> str | None`.

### Step 5 — `tracker.py` + `main.py`: cumulative failure counter, drop after 3

Add `failure_count INTEGER DEFAULT 0` column to `applications` (migration:
`ALTER TABLE applications ADD COLUMN failure_count INTEGER DEFAULT 0`,
guarded by `try/except OperationalError` so it's idempotent).

In `tracker.upsert_application`: when `app.status == "failed"`, increment
the existing row's `failure_count` (read-then-write inside the same
transaction). When status is `applied` or `skipped`, reset to 0.

New helper `tracker.permanently_failed_urls() -> set[str]` returning all
URLs with `failure_count >= 3`.

In `scraper/seek.py::_filter_seen`: drop URLs in
`permanently_failed_urls()` too.

In `main.py::recover_orphans`: skip orphans whose `failure_count >= 3`,
mark them `status="failed"`, `notes="Auto-skipped: 3+ failures"`.

Bonus: add `models.Application.failure_count: int = 0` field.

### Step 6 — Per-application JSONL log for root-cause analysis

New file `errors/applications.jsonl`. One record per *apply attempt*
(not per recovery iteration). Schema:

```json
{
  "ts": "2026-04-30T11:30:00",
  "url": "...", "title": "...", "company": "...",
  "outcome": "applied|failed|skipped",
  "match_score": 47,
  "steps_seen": ["choose documents", "answer employer questions", "review"],
  "questions_answered": [
    {"question": "...", "field_kind": "input:radio",
     "answer": "No", "source": "hard-rule|alias|claude|fallback"},
    ...
  ],
  "validation_errors_seen": [...],
  "final_error": "...",   // null if outcome=applied
  "duration_seconds": 47
}
```

Implementation:
- Add `ApplyJournal` dataclass (or just a dict) initialised at the top of
  `apply_seek_quick`. Pass it through to `_handle_form_step`,
  `_answer_radio_group`, `_resolve_validation_errors`, `_claude_answer`.
- Each picker appends a `questions_answered` entry tagged with `source`.
- On exit (success or exception), write one JSONL line.

Keep `stuck_questions.jsonl` as-is (legacy). Both files exist; new file is
the authoritative per-attempt record.

### Step 7 — Tests

`tests/test_radio_hard_rules.py`: unit-test the new hard-rule matcher with
real question labels and option lists from the JSONL log:

- "Are you an Australian Citizen, with a minimum NV1 Security Clearance?"
  + ["Yes", "No"] → "No"
- "Are you legally entitled to work in Australia?" + ["Yes", "No"] → "Yes"
- "Do you have a security clearance at minimum NV1." + ["Yes", "No"] → "No"
- "Visa Status" + ["Australian Citizen", "Permanent Resident", "485
  Temporary Graduate Visa", "Other"] → "485 Temporary Graduate Visa"
- "Visa Status" + ["Citizen", "PR", "Visa with no restrictions"] → "Visa
  with no restrictions"

`tests/test_validation_error_extraction.py`: feed a saved Seek HTML
fixture with two validation errors in two different fieldsets, assert
`_find_validation_errors` returns the right `(legend, error)` pair for
each.

`tests/test_failure_counter.py`: insert a fake app, mark it failed 3x,
verify `permanently_failed_urls()` includes it, verify `_filter_seen`
drops it.

---

## Rollout order

1. Step 5 (failure counter + scraper filter) — **deploy first**. Stops
   bleeding immediately by preventing the 30-40 wasted Claude calls per
   broken job.
2. Step 1 (validation-error fieldset extraction).
3. Step 2 + Step 3 + Step 4 (hard rules + visa aliases).
4. Step 6 (per-application journal).
5. Step 7 (tests — written alongside each change, not after).

After deploy, watch `errors/applications.jsonl` for 24h. Any new stuck-
question pattern → another hard rule, not a Claude tweak.

## Verification (after implementation, before claiming done)

```bash
# 1. Migration ran without error
venv/bin/python -c "import tracker, sqlite3; tracker.init_db(); \
  print([r for r in sqlite3.connect('jobs.db').execute('PRAGMA table_info(applications)').fetchall()])"

# 2. permanently_failed_urls finds the 4 known-broken jobs
venv/bin/python -c "import tracker; print(tracker.permanently_failed_urls())"

# 3. Hard-rule unit tests pass
venv/bin/pytest tests/test_radio_hard_rules.py -v

# 4. Restart bot in nonstop relevance mode and watch for new failures
tail -f errors/applications.jsonl | grep '"outcome":"failed"'
```

Done = next 10 successful submits show no stuck-question events for the
four candidate-fact questions, AND the 4 known-broken URLs above are
filtered out at scrape time (zero new log events for them).

## Out of scope (explicitly NOT in this plan)

- Rewriting the scoring or tailoring pipeline.
- Multi-tenancy / EC2 (those are Plan 2).
- Web dashboard (Plan 3).
- Resetting historical `failure_count` on visa-detail changes — fresh
  scrapes naturally re-evaluate after the fix lands.
