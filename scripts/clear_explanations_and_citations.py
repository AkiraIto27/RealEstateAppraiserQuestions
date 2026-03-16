#!/usr/bin/env python3

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DIST_DIRS = [ROOT / "dist", ROOT / "dist_with_ai"]


def clear_csv_fields() -> None:
    changed_files = 0
    changed_rows = 0

    for csv_path in sorted(DATA_DIR.glob("r0[3-7]_*.csv")):
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames
            rows = list(reader)

        file_changed = False
        for row in rows:
            if row.get("explanation", "") or row.get("law_citations", ""):
                row["explanation"] = ""
                row["law_citations"] = ""
                changed_rows += 1
                file_changed = True

        if file_changed:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            changed_files += 1

    print(f"[clear_explanations] CSV updated files={changed_files} rows={changed_rows}")


def clear_bundle_fields(dist_dir: Path) -> None:
    bundles_dir = dist_dir / "bundles"
    manifest_path = dist_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_map = {bundle["id"]: bundle for bundle in manifest.get("bundles", [])}
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    changed_bundles = 0

    for bundle_path in sorted(bundles_dir.glob("r0[3-7].jsonl.gz")):
        with gzip.open(bundle_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]

        changed = False
        for record in records:
            if record.get("explanation", "") != "":
                record["explanation"] = ""
                changed = True
            if record.get("law_citations") != []:
                record["law_citations"] = []
                changed = True

        if changed:
            payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
            with gzip.open(bundle_path, "wt", encoding="utf-8") as fh:
                fh.write(payload)
            changed_bundles += 1

        bundle_id = bundle_path.stem.replace(".jsonl", "")
        if bundle_id in bundle_map:
            blob = bundle_path.read_bytes()
            bundle_map[bundle_id]["size"] = len(blob)
            bundle_map[bundle_id]["sha256"] = hashlib.sha256(blob).hexdigest()
            bundle_map[bundle_id]["updated_at"] = now_iso

    manifest["generated_at"] = now_iso
    manifest["bundles"] = sorted(bundle_map.values(), key=lambda bundle: bundle["id"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[clear_explanations] {dist_dir.name} updated bundles={changed_bundles}")


def main() -> None:
    clear_csv_fields()
    for dist_dir in DIST_DIRS:
        clear_bundle_fields(dist_dir)


if __name__ == "__main__":
    main()
