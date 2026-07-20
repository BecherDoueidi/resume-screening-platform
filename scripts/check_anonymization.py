"""Verify no planted demographic marker survives in the anonymized payloads.

Usage: python scripts/check_anonymization.py
Requires: make_sample_resumes.py run first, then the pipeline with --dump-anonymized.
Exit code 0 = clean, 1 = leaks found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER_FILE = ROOT / "data" / "resumes" / "_planted_markers.txt"
ANON_DIR = ROOT / "output" / "anonymized"

# Generic words that appear in institution names but aren't identifying on their own.
GENERIC = {
    "university",
    "college",
    "institute",
    "community",
    "state",
    "technical",
    "school",
    "of",
    "de",
    "city",
    "united",
    "kingdom",
    "national",
    "vocational",
    "instituto",
    "tecnológico",
    "massachusetts",
    "technology",
    "saudi",
    "arabia",
}


def main() -> int:
    markers: set[str] = set()
    for line in MARKER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        markers.add(line)
        # Individual words too, to catch partial leaks (e.g. surname surviving NER).
        for word in line.replace(",", " ").split():
            if len(word) > 3 and word.lower() not in GENERIC and not any(c.isdigit() for c in word):
                markers.add(word)

    leaks: list[tuple[str, str]] = []
    files = sorted(ANON_DIR.glob("*.txt"))
    if not files:
        print(f"No anonymized payloads in {ANON_DIR} - run the pipeline with --dump-anonymized first.")
        return 1
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in markers:
            # Word-boundary match so "King" doesn't hit inside "tracking".
            if re.search(r"(?<!\w)" + re.escape(m) + r"(?!\w)", text, re.IGNORECASE):
                leaks.append((f.name, m))

    if leaks:
        print(f"LEAKED {len(leaks)} marker(s):")
        for fn, m in leaks:
            print(f"  {fn}: {m!r}")
        return 1
    print(f"CLEAN: none of {len(markers)} markers/word-fragments found in {len(files)} anonymized payloads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
