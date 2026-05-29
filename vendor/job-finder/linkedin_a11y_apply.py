"""
A11y-tree based LinkedIn apply pipeline.
Karpathy minimum: use CDP Accessibility.getFullAXTree to enumerate fields,
use Playwright's get_by_role to act. Works regardless of iframe/shadow DOM.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from claude_cli import DEFAULT_MODEL, claude_complete
from utils import load_profile

# Page-chrome labels we MUST ignore (LinkedIn nav/footer/search-results filters)
CHROME_LABELS = {
    "search", "select language", "tap to toggle setting",
    "select language\nenglish (english)",
    "describe the job you want", "search jobs", "easy apply",
    "under 10 applicants", "in my network", "jobs",
}

# Substrings — if found in a label, the field is part of search/filter chrome
CHROME_LABEL_SUBSTRINGS = (
    "filter by", "describe the job", "search jobs", "people search",
)


async def _apply_modal_open(page) -> bool:
    """Check whether the Easy Apply modal is currently visible."""
    n = await page.locator(
        '[role="dialog"]:has-text("Apply to"), div:has(> h2:has-text("Apply to"))'
    ).count()
    return n > 0


def _prop(node, key):
    v = node.get(key)
    if isinstance(v, dict):
        return str(v.get("value", ""))
    return str(v or "")


async def _fetch_select_options(page, label: str) -> list[str]:
    """Get options of a <select> located by accessible name. CDP a11y often
    omits combobox options; fetch them via Playwright as a fallback."""
    try:
        sel = page.get_by_role("combobox", name=label).first
        opts = await sel.locator("option").all_text_contents()
        return [o.strip() for o in opts
                if o.strip() and not o.strip().lower().startswith("select")]
    except Exception:
        return []


async def get_a11y_form_fields(page) -> list[dict]:
    """Enumerate form fields via CDP a11y tree. Returns list of:
    {role, label, value, node_id, options: [str]} where options is [] for text/textbox."""
    client = await page.context.new_cdp_session(page)
    await client.send("Accessibility.enable")
    result = await client.send("Accessibility.getFullAXTree")
    await client.detach()
    nodes = result.get("nodes", [])

    # Build id index for option/parent lookups
    by_id = {n["nodeId"]: n for n in nodes}

    def _walk_up_for_group_name(node, max_depth=8):
        """Find the nearest ancestor whose role is group/radiogroup with a name.
        Returns the group's accessible name (the actual question text)."""
        cur = node
        for _ in range(max_depth):
            parent_id = cur.get("parentId")
            if not parent_id:
                return ""
            parent = by_id.get(parent_id)
            if not parent:
                return ""
            p_role = _prop(parent, "role")
            p_name = _prop(parent, "name").strip()
            if p_role in {"group", "radiogroup"} and p_name:
                return p_name
            cur = parent
        return ""

    fields = []
    seen_labels = set()
    radio_groups_seen = set()  # parent_id of radio groups already added
    for n in nodes:
        role = _prop(n, "role")
        if role not in {"combobox", "textbox", "checkbox", "radio", "spinbutton"}:
            continue
        name = _prop(n, "name").strip()
        if not name:
            continue
        if name.lower() in CHROME_LABELS:
            continue
        if any(s in name.lower() for s in CHROME_LABEL_SUBSTRINGS):
            continue
        if "resume" in name.lower() and ("select " in name.lower() or "deselect" in name.lower()):
            continue

        # Collapse radio groups: walk up to find the parent group name (question).
        # If found, emit ONE field with all radios as options instead of one
        # field per radio.
        if role == "radio":
            parent_id = n.get("parentId")
            # Walk up to a [radiogroup] / [group] with a name
            group_node = None
            cur = n
            for _ in range(8):
                pid = cur.get("parentId")
                if not pid:
                    break
                p = by_id.get(pid)
                if not p:
                    break
                if _prop(p, "role") in {"group", "radiogroup"} and _prop(p, "name").strip():
                    group_node = p
                    break
                cur = p
            if group_node:
                gid = group_node["nodeId"]
                if gid in radio_groups_seen:
                    continue
                radio_groups_seen.add(gid)
                # Collect all radio option names + checked value
                option_names = []
                checked_val = ""
                for cid in group_node.get("childIds", []):
                    c = by_id.get(cid)
                    if c and _prop(c, "role") == "radio":
                        opt_name = _prop(c, "name").strip()
                        if opt_name:
                            option_names.append(opt_name)
                            # CDP marks checked radios via 'checked' property
                            for prop in c.get("properties", []):
                                if prop.get("name") == "checked" and prop.get("value", {}).get("value"):
                                    checked_val = opt_name
                fields.append({
                    "role": "radio_group",
                    "label": _prop(group_node, "name").strip(),
                    "value": checked_val,
                    "options": option_names,
                })
                continue
            # If no parent group, fall through to the per-radio path below

        value = _prop(n, "value").strip()
        options = []
        for child_id in n.get("childIds", []):
            child = by_id.get(child_id)
            if child and _prop(child, "role") in {"menuitem", "option"}:
                opt_name = _prop(child, "name").strip()
                if opt_name and opt_name.lower() not in {"select an option", "select"}:
                    options.append(opt_name)
        key = (role, name)
        if key in seen_labels:
            continue
        seen_labels.add(key)
        fields.append({
            "role": role, "label": name, "value": value,
            "options": options,
        })

    # Fill in missing combobox options via Playwright (a11y tree often omits them)
    for f in fields:
        if f["role"] == "combobox" and not f["options"]:
            f["options"] = await _fetch_select_options(page, f["label"])
    return fields


def hard_rule_answer(field: dict, candidate: dict) -> str | None:
    """Return a deterministic answer for known question patterns, else None."""
    label = field["label"].lower()
    role = field["role"]

    # Visa / sponsorship
    if "sponsor" in label:
        return "No"
    # Working rights
    if "working rights" in label or "right to work" in label:
        if role == "combobox" and field["options"]:
            for opt in field["options"]:
                if "no restrictions" in opt.lower() or "full working" in opt.lower():
                    return opt
            for opt in field["options"]:
                if "visa" in opt.lower() and "no restrict" in opt.lower():
                    return opt
        return "Yes"
    # Notice period
    if "notice" in label and ("required" in label or "give" in label):
        if field["options"]:
            for opt in field["options"]:
                if "2 week" in opt.lower():
                    return opt
        return "2 weeks"
    # Salary
    if "salary" in label or "compensation" in label or "remuneration" in label:
        return "110000"
    # Holidays
    if "holiday" in label or "vacation" in label or "leave booked" in label:
        return "No"
    # Work environment / arrangement
    if "work environment" in label or "work arrangement" in label or "work setting" in label:
        return "Hybrid"
    # Location
    if label in ("location (city)", "city", "location"):
        return "Sydney, New South Wales, Australia"
    # Years of experience (any tech)
    if "years of experience" in label or "years of work experience" in label or "how many years" in label:
        return "3"
    # Phone number
    if "phone" in label and "country" not in label:
        return candidate.get("phone", "+61491621148").replace("+61", "")
    # Email
    if "email" in label:
        return candidate.get("email", "sagarvd130@gmail.com")
    return None


async def claude_answer(field: dict, profile: str, job_title: str, job_company: str) -> str:
    """Answer a LinkedIn application screening question via the `claude` CLI."""
    options_str = ""
    if field["options"]:
        options_str = "\nOPTIONS (pick exactly one verbatim):\n- " + "\n- ".join(field["options"])
    constraint = ""
    if "years" in field["label"].lower() or "how many" in field["label"].lower():
        constraint = "\nReply with ONLY a single integer (no units, no text)."
    prompt = (
        f"Answer this LinkedIn application question on behalf of the candidate.\n\n"
        f"--- CANDIDATE PROFILE ---\n{profile}\n--- END PROFILE ---\n\n"
        f"Job: {job_title} @ {job_company}\n"
        f"Question: {field['label']}\n"
        f"Field type: {field['role']}{options_str}{constraint}\n\n"
        f"Reply with ONLY the answer text. No preamble. No explanation."
    )
    text = await claude_complete(
        system="Answer LinkedIn application screening questions. Reply with ONLY the answer text.",
        user=prompt,
        model=DEFAULT_MODEL,
    )
    return text.strip()


async def fill_field(page, field: dict, answer: str) -> bool:
    """Use Playwright get_by_role to apply the answer. Returns True on success."""
    role, label = field["role"], field["label"]
    try:
        if role == "combobox":
            # Special case: Location field — fill with the exact string,
            # dismiss the dropdown by pressing Escape (don't auto-pick).
            label_lower = label.lower()
            if "location" in label_lower or label_lower == "city":
                loc = page.get_by_role("combobox", name=label).first
                try:
                    await loc.click(timeout=3000)
                    await loc.fill(answer, timeout=3000)
                    await loc.press("Escape")  # close dropdown without selecting
                    await loc.blur()
                    print(f"     ✅ location '{label[:60]}' → {answer!r} (literal fill)")
                    return True
                except Exception as e:
                    print(f"     ⚠️ location fill failed: {e}")
                    return False
            loc = page.get_by_role("combobox", name=label)
            for kw in (
                {"label": answer},
                {"label": answer.strip()},
                {"value": answer},
            ):
                try:
                    await loc.select_option(**kw, timeout=3000)
                    print(f"     ✅ combobox '{label[:60]}' → {answer!r}")
                    return True
                except Exception:
                    continue
            # Last-resort: fill (some are autocomplete textboxes)
            try:
                await loc.click(timeout=3000)
                await loc.fill(answer, timeout=3000)
                print(f"     ✅ combobox typed '{label[:60]}' → {answer!r}")
                return True
            except Exception as e:
                print(f"     ❌ combobox '{label[:60]}': {e}")
                return False
        if role in {"textbox", "spinbutton"}:
            loc = page.get_by_role(role, name=label)
            await loc.click(timeout=3000)
            await loc.fill(answer, timeout=3000)
            print(f"     ✅ {role} '{label[:60]}' → {answer!r}")
            return True
        if role in ("radio", "radio_group"):
            # answer is the option text (e.g. "Yes" or "No"); click that label
            sel = f'label[data-test-text-selectable-option__label="{answer}"]'
            try:
                await page.locator(sel).first.click(timeout=3000)
            except Exception:
                try:
                    await page.locator(sel).first.dispatch_event("click")
                except Exception:
                    await page.get_by_role("radio", name=answer).first.click(force=True, timeout=3000)
            print(f"     ✅ radio_group '{label[:50]}' → {answer!r}")
            return True
        if role == "checkbox":
            if answer.strip().lower() in label.strip().lower() or label.strip().lower() in answer.strip().lower():
                sel = f'label[data-test-text-selectable-option__label="{label}"]'
                try:
                    await page.locator(sel).first.click(timeout=3000)
                except Exception:
                    try:
                        await page.locator(sel).first.dispatch_event("click")
                    except Exception:
                        await page.get_by_role("checkbox", name=label).click(force=True, timeout=3000)
                print(f"     ✅ checkbox '{label[:60]}' clicked")
                return True
            return False
    except Exception as e:
        print(f"     ❌ failed {role} '{label[:60]}': {e}")
    return False


async def answer_all_fields(page, fields: list[dict], candidate: dict,
                              profile: str, job_title: str, job_company: str) -> int:
    """Apply hard rules + Claude to every field. Returns count of fields filled."""
    filled = 0
    for f in fields:
        # 'Select an option' is the placeholder, not a real value
        if f["value"] and f["value"].strip().lower() not in ("select an option", "select"):
            print(f"   ⊘ pre-filled: [{f['role']}] {f['label'][:60]!r} = {f['value'][:40]!r}")
            continue
        ans = hard_rule_answer(f, candidate)
        if ans is None:
            ans = await claude_answer(f, profile, job_title, job_company)
        if await fill_field(page, f, ans):
            filled += 1
        await asyncio.sleep(0.3)
    return filled


# ==== Helpers for the orchestrator ====

async def verify_applied_on_tracker(page, job_id: str) -> bool:
    """Navigate to LinkedIn's official Applied tracker and check if job_id is there.
    Returns True if verified, False if not (false positive)."""
    try:
        await page.goto("https://www.linkedin.com/jobs-tracker/?stage=applied",
                        wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(5)
        body = (await page.content()).lower()
        return job_id in body
    except Exception as e:
        print(f"   ⚠️ verify failed: {e}")
        return False


async def walk_and_apply(page, job, resume_pdf: str, cover_pdf: str | None,
                          candidate: dict, max_steps: int = 12) -> str:
    """Walk LinkedIn Easy Apply modal end-to-end using a11y enumeration.
    Returns 'applied' / 'stuck' / 'max_steps' / 'no_easy_apply' / 'already_applied'."""
    profile = load_profile()

    easy = page.locator('button[aria-label*="Easy Apply"], a[aria-label*="Easy Apply"]').first
    try:
        await easy.wait_for(timeout=10000)
    except Exception:
        return "no_easy_apply"
    aria = (await easy.get_attribute("aria-label") or "").lower()
    txt = (await easy.inner_text() or "").lower()
    if "applied" in (aria + txt) and "easy apply" not in (aria + txt):
        return "already_applied"
    await easy.click()
    await asyncio.sleep(3)

    handled_files: set[int] = set()
    for step in range(1, max_steps + 1):
        # 1. Submit reachable?
        sub = page.locator('button[aria-label*="Submit application"]')
        if await sub.count() > 0:
            print(f"\n   🏁 Step {step}: clicking Submit")
            await sub.first.click()
            await asyncio.sleep(4)
            return "applied"

        # 2. Upload to any unhandled file inputs (resume + cover letter)
        n_files = await page.locator('input[type=file]').count()
        for i in range(n_files):
            if i in handled_files:
                continue
            el = page.locator('input[type=file]').nth(i)
            label_kind = await el.evaluate("""el => {
                let p = el.parentElement;
                for (let k = 0; k < 6 && p; k++) {
                    const t = (p.innerText || '').toLowerCase();
                    if (t.includes('cover')) return 'cover';
                    if (t.includes('resume')) return 'resume';
                    p = p.parentElement;
                }
                return '';
            }""")
            target = cover_pdf if (label_kind == "cover" and cover_pdf) else resume_pdf
            try:
                await el.set_input_files(target)
                handled_files.add(i)
                print(f"   📎 step {step}: uploaded {Path(target).name} → input #{i} ({label_kind})")
            except Exception as e:
                print(f"   ⚠️ upload #{i} failed: {e}")
                handled_files.add(i)
        if n_files and handled_files:
            await asyncio.sleep(4)

        # 3. Enumerate + answer fields via a11y — ONLY when modal is open,
        #    otherwise we'd pick up search-page filters and the search box.
        if await _apply_modal_open(page):
            fields = await get_a11y_form_fields(page)
            if fields:
                await answer_all_fields(page, fields, candidate, profile,
                                         job.title, job.company)
                await asyncio.sleep(1.5)
        else:
            print(f"   ⚠️ step {step}: apply modal not detected — skipping field scrape")

        # 4. Click Next/Review/Continue
        nxt = page.locator(
            'button[aria-label*="Continue to next step"], '
            'button[aria-label*="Review"], '
            'button:has-text("Next"), button:has-text("Review")'
        )
        if await nxt.count() == 0:
            return "stuck"
        try:
            await nxt.first.click(timeout=5000)
            await asyncio.sleep(2.5)
        except Exception:
            return "stuck"
    return "max_steps"
