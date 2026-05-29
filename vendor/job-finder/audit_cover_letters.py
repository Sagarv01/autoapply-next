"""One-off audit: read the last N CoverLetter PDFs and classify each as
DISASTER (refusal/junk shipped to recruiter), TEMPLATE (safe generic), or
GOOD (LLM-tailored). Run: venv/bin/python audit_cover_letters.py [N]
"""
import sys
from pathlib import Path

from pypdf import PdfReader

from tailorer import _REFUSAL_MARKERS

OUTPUT_DIR = Path("output")
TEMPLATE_FINGERPRINT = "i'm an aws-certified devops and cloud engineer with 5+ years"

# Phrases that should NEVER appear in a real cover letter — these are the
# system prompt's internal categories / meta-reasoning leaking into the body.
_META_LEAK_MARKERS = (
    "strong match",
    "adjacent role",
    "adjacency",
    "i will write based on",
    "i will treat this",
    "i will treat the role",
    "the candidate's profile",
    "the profile says",
    "the profile contains",
    "internal notes",
    "step 1",
    "step 2",
    "(a) strong match",
    "(b) adjacent",
    "(c) gap",
    "as a strong match",
    "as adjacent",
    "as a gap",
    "given the candidate",
    "the candidate has",
    "candidate profile",
)


def normalize(text: str) -> str:
    """Collapse whitespace so PDF line breaks don't break substring matches."""
    return " ".join(text.split())


def classify(text: str) -> str:
    norm = normalize(text)
    lower = norm.lower()
    if any(m in lower for m in _REFUSAL_MARKERS):
        return "DISASTER"
    if "i cannot" in lower and "job description" in lower:
        return "DISASTER"
    if "sagar verma" not in lower or len(norm) < 400:
        return "DISASTER"
    if any(m in lower for m in _META_LEAK_MARKERS):
        return "META_LEAK"
    if TEMPLATE_FINGERPRINT in lower:
        return "TEMPLATE"
    return "GOOD"


def read_pdf(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        return f"__READ_ERROR__: {type(e).__name__}: {e}"


def main(n: int) -> None:
    pdfs = sorted(OUTPUT_DIR.glob("CoverLetter_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    print(f"Auditing {len(pdfs)} most recent cover letters from {OUTPUT_DIR.resolve()}\n")

    counts = {"DISASTER": 0, "META_LEAK": 0, "TEMPLATE": 0, "GOOD": 0, "READ_ERROR": 0}
    samples: dict[str, list[tuple[str, str]]] = {"DISASTER": [], "META_LEAK": [], "TEMPLATE": [], "GOOD": []}
    sample_caps = {"DISASTER": 3, "META_LEAK": 5, "TEMPLATE": 2, "GOOD": 2}

    for path in pdfs:
        text = read_pdf(path)
        if text.startswith("__READ_ERROR__"):
            counts["READ_ERROR"] += 1
            continue
        verdict = classify(text)
        counts[verdict] += 1
        if len(samples[verdict]) < sample_caps[verdict]:
            samples[verdict].append((path.name, normalize(text)[:400]))

    total_classified = counts["DISASTER"] + counts["META_LEAK"] + counts["TEMPLATE"] + counts["GOOD"]
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  DISASTER (refusal shipped) : {counts['DISASTER']:4d}  ({100*counts['DISASTER']/max(total_classified,1):.1f}%)")
    print(f"  META_LEAK (LLM thoughts)   : {counts['META_LEAK']:4d}  ({100*counts['META_LEAK']/max(total_classified,1):.1f}%)")
    print(f"  TEMPLATE (safe generic)    : {counts['TEMPLATE']:4d}  ({100*counts['TEMPLATE']/max(total_classified,1):.1f}%)")
    print(f"  GOOD (LLM-tailored OK)     : {counts['GOOD']:4d}  ({100*counts['GOOD']/max(total_classified,1):.1f}%)")
    print(f"  READ_ERROR                 : {counts['READ_ERROR']:4d}")
    print(f"  TOTAL                      : {len(pdfs):4d}")
    print()
    print(f"BAD shipped (DISASTER + META_LEAK): {counts['DISASTER']+counts['META_LEAK']:4d}  "
          f"({100*(counts['DISASTER']+counts['META_LEAK'])/max(total_classified,1):.1f}%)")
    print()

    for verdict in ("DISASTER", "META_LEAK", "TEMPLATE", "GOOD"):
        if not samples[verdict]:
            continue
        print("=" * 70)
        print(f"{verdict} SAMPLES (showing up to {sample_caps[verdict]} of {counts[verdict]})")
        print("=" * 70)
        for name, snippet in samples[verdict]:
            print(f"\n--- {name} ---")
            print(snippet)
        print()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    main(n)
