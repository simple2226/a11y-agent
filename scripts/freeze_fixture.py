#!/usr/bin/env python3
"""
Freeze a live page into evals/fixtures/ so your demo never depends on a
government server staying up on Saturday night.

    python scripts/freeze_fixture.py https://example.ac.in/ example-home
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audit.mirror import mirror

url, name = sys.argv[1], sys.argv[2]
mirrored = mirror(url)
target = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / f"{name}.html"
target.write_text(mirrored.html, encoding="utf-8")
print(f"wrote {target} ({len(mirrored.html)} bytes) from {mirrored.final_url}")
for note in mirrored.notes:
    print(f"  {note}")
