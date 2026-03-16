#!/usr/bin/env python3

from __future__ import annotations

import csv
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TEXT_FIELDS = ("statement", "choice1", "choice2", "choice3", "choice4", "choice5")
PAGE_MARKER_RE = re.compile(r"^-\s*\d+\s*-$")


def clean_text(value: str) -> str:
    lines = value.splitlines()
    cleaned = [line for line in lines if not PAGE_MARKER_RE.fullmatch(line.strip())]
    return "\n".join(cleaned).strip()


def main() -> int:
    changed_files = 0
    changed_fields = 0

    for csv_path in sorted(DATA_DIR.glob("*.csv")):
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))

        if not rows:
            continue

        fieldnames = list(rows[0].keys())
        file_changed = False

        for row in rows:
            for field in TEXT_FIELDS:
                original = row.get(field, "")
                cleaned = clean_text(original)
                if cleaned != original:
                    row[field] = cleaned
                    changed_fields += 1
                    file_changed = True

        if not file_changed:
            continue

        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        changed_files += 1
        print(f"[clean] {csv_path.name}")

    print(f"[clean] changed_files={changed_files} changed_fields={changed_fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
