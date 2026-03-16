#!/usr/bin/env python3

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = sorted((ROOT / "data").glob("r0[3-7]_*.csv"))
DIST_BUNDLES_DIR = ROOT / "dist" / "bundles"
DIST_WITH_AI_BUNDLES_DIR = ROOT / "dist_with_ai" / "bundles"
TOP_LEVEL_MARKER_RE = re.compile(r"^[イロハニホ](?:[\s　]|$)")
BULLET_RE = re.compile(r"^[・●○◯■□◆◇]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    return CONTROL_CHAR_RE.sub("", text)


def split_embedded_markers(text: str) -> str:
    text = re.sub(r"(。)(?=(?:</border>)?[イロハニホ](?:[\s　]|$))", r"\1\n", text)
    text = re.sub(r"(?<!\n)(</border>[イロハニホ](?:[\s　]|$))", r"\n\1", text)
    return text


def normalize_statement(text: str) -> str:
    cleaned = text.replace("\\n", "\n")
    cleaned = strip_control_chars(cleaned).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = split_embedded_markers(cleaned)

    lines = []
    for raw_line in cleaned.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("</border>"):
            line = line[len("</border>"):].strip()
        lines.append(line)

    if not lines:
        return ""

    paragraphs: list[str] = []
    current = ""

    for line in lines:
        if TOP_LEVEL_MARKER_RE.match(line) or BULLET_RE.match(line):
            if current:
                paragraphs.append(current)
            current = line
            continue

        if not current:
            current = line
            continue

        if current in {"・", "●", "○", "◯", "■", "□", "◆", "◇"}:
            current = f"{current} {line}"
        else:
            current += line

    if current:
        paragraphs.append(current)

    normalized_paragraphs = []
    for index, paragraph in enumerate(paragraphs):
        paragraph = re.sub(r"^([イロハニホ])([^\s　])", r"\1 \2", paragraph).strip()
        normalized_paragraphs.append(paragraph if index == 0 else f"</border>{paragraph}")

    return "\\n".join(normalized_paragraphs)


def make_key(year: str, subject: str, question_no: str | int) -> str:
    return f"{year}::{subject.strip()}::{question_no}"


def rewrite_csvs() -> dict[str, str]:
    statement_by_key: dict[str, str] = {}
    changed_files = 0
    changed_rows = 0

    for csv_path in DATA_FILES:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames
            rows = list(reader)

        file_changed = False
        for row in rows:
            normalized = normalize_statement(row.get("statement", ""))
            if normalized != row.get("statement", ""):
                row["statement"] = normalized
                changed_rows += 1
                file_changed = True
            key = make_key(row.get("year", ""), row.get("subject", ""), row.get("question_no", ""))
            statement_by_key[key] = row["statement"]

        if file_changed:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            changed_files += 1

    print(f"[normalize_statement_breaks] updated CSV files: {changed_files}, rows: {changed_rows}")
    return statement_by_key


def update_dist_with_ai_bundles(statement_by_key: dict[str, str]) -> None:
    changed_bundle_ids: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for bundle_path in sorted(DIST_WITH_AI_BUNDLES_DIR.glob("r0[3-7].jsonl.gz")):
        with gzip.open(bundle_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]

        changed = False
        for record in records:
            key = make_key(record.get("year", ""), record.get("subject", ""), record.get("question_no", ""))
            new_statement = statement_by_key.get(key)
            if new_statement and new_statement != record.get("statement", ""):
                record["statement"] = new_statement
                changed = True

        if not changed:
            continue

        payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        with gzip.open(bundle_path, "wt", encoding="utf-8") as fh:
            fh.write(payload)
        changed_bundle_ids.append(bundle_path.stem.replace(".jsonl", ""))

    manifest_path = ROOT / "dist_with_ai" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_map = {bundle["id"]: bundle for bundle in manifest.get("bundles", [])}

    for bundle_path in sorted(DIST_WITH_AI_BUNDLES_DIR.glob("r0[3-7].jsonl.gz")):
        bundle_id = bundle_path.stem.replace(".jsonl", "")
        if bundle_id not in bundle_map:
            continue
        blob = bundle_path.read_bytes()
        bundle_map[bundle_id]["size"] = len(blob)
        bundle_map[bundle_id]["sha256"] = hashlib.sha256(blob).hexdigest()
        if bundle_id in changed_bundle_ids:
            bundle_map[bundle_id]["updated_at"] = now_iso

    manifest["generated_at"] = now_iso
    manifest["bundles"] = sorted(bundle_map.values(), key=lambda bundle: bundle["id"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[normalize_statement_breaks] updated dist_with_ai bundles: {len(changed_bundle_ids)}")


def main() -> None:
    statement_by_key = rewrite_csvs()
    update_dist_with_ai_bundles(statement_by_key)


if __name__ == "__main__":
    main()
