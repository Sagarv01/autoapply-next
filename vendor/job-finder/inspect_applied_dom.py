"""Dump the DOM structure of /my-activity/applied-jobs so we can find a stable
selector for individual job cards / job IDs."""
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://au.seek.com/my-activity/applied-jobs"
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path("errors/inspect_applied")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()

        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(6)

        info = await page.evaluate("""() => {
            const all_links = Array.from(document.querySelectorAll('a')).map(a => ({
                href: a.getAttribute('href') || '',
                text: a.innerText?.trim().slice(0, 80) || '',
                'data-automation': a.getAttribute('data-automation') || '',
            })).filter(a => a.href && !a.href.startsWith('#'));
            const automations = {};
            document.querySelectorAll('[data-automation]').forEach(el => {
                const a = el.getAttribute('data-automation');
                automations[a] = (automations[a] || 0) + 1;
            });
            const cards = Array.from(document.querySelectorAll('article, [role=listitem], li')).map(el => ({
                tag: el.tagName,
                automation: el.getAttribute('data-automation') || '',
                text: el.innerText?.trim().slice(0, 200) || '',
                links: Array.from(el.querySelectorAll('a')).map(a => a.getAttribute('href')).filter(Boolean),
            })).filter(c => c.text && c.text.length > 30);
            return {all_links, automations, cards: cards.slice(0, 20)};
        }""")

        (OUT / "links.json").write_text(json.dumps(info['all_links'], indent=2))
        (OUT / "automations.json").write_text(json.dumps(info['automations'], indent=2))
        (OUT / "cards.json").write_text(json.dumps(info['cards'], indent=2))
        (OUT / "page.html").write_text(await page.content())
        await page.screenshot(path=str(OUT / "page.png"), full_page=True)

        print(f"\nTotal anchors: {len(info['all_links'])}")
        print(f"Anchors containing '/job/': {sum(1 for a in info['all_links'] if '/job/' in a['href'])}")
        print(f"Anchors containing '/my-activity/': {sum(1 for a in info['all_links'] if '/my-activity/' in a['href'])}")
        print(f"Anchors containing 'application': {sum(1 for a in info['all_links'] if 'application' in a['href'])}")
        print()
        print("Top 15 hrefs (deduped by pattern):")
        seen = set()
        for a in info['all_links']:
            pat = re.sub(r'\d+', 'N', a['href'])
            if pat in seen: continue
            seen.add(pat)
            print(f"  {a['href'][:90]}  text='{a['text'][:50]}'")
            if len(seen) >= 15: break
        print()
        print("Top data-automation attrs:")
        for k, v in sorted(info['automations'].items(), key=lambda x: -x[1])[:15]:
            print(f"  {v:3d}× {k}")
        print()
        print(f"Sample cards: {len(info['cards'])}")
        for c in info['cards'][:6]:
            print(f"  {c['tag']} auto='{c['automation']}' links={c['links']}")
            print(f"      text: {c['text'][:120]}")
        await asyncio.sleep(20)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
