import argparse
import csv
import gzip
import json
import re
import subprocess
from pathlib import Path


ROOT = Path("/Users/itouakira/Documents/GitHub/RealEstateAppraiserQuestions")
YEAR_TO_SOURCE = {
    "r04": "2022",
    "r05": "2023",
    "r06": "2024",
    "r07": "2025",
}
SUBJECT_CONFIG = {
    "gyousei": {
        "csv_name": "gyousei",
        "pdf_name": "gyousei_answer.pdf",
    },
    "kanteihyoka": {
        "csv_name": "kanteihyoka",
        "pdf_name": "kantei_answer.pdf",
    },
}
ANSWER_RE = re.compile(r"\(([1-5])\)")


def extract_pdf_text(pdf_path: Path) -> str:
    swift_code = f"""
import PDFKit
import Foundation

let url = URL(fileURLWithPath: "{pdf_path}")
guard let document = PDFDocument(url: url) else {{
    fatalError("failed to open pdf")
}}

for index in 0..<document.pageCount {{
    if let text = document.page(at: index)?.string {{
        print(text)
    }}
}}
"""
    result = subprocess.run(
        [
            "swift",
            "-module-cache-path",
            "/tmp/swiftmodulecache",
            "-e",
            swift_code,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def load_source_text(year: str, subject: str) -> str:
    source_year = YEAR_TO_SOURCE[year]
    config = SUBJECT_CONFIG[subject]
    pdf_path = ROOT / "raw_data" / source_year / config["pdf_name"]
    snapshot_path = ROOT / "scripts" / f"{year}_{subject}_answer_source.txt"
    text = extract_pdf_text(pdf_path).strip()
    snapshot_path.write_text(text, encoding="utf-8")
    return text


def parse_answers(text: str) -> list[str]:
    answers = ANSWER_RE.findall(text)
    if len(answers) != 40:
        raise ValueError(f"Expected 40 answers, got {len(answers)}")
    return answers


def load_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv_rows(csv_path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sync_bundle(bundle_path: Path, rows_by_id: dict[str, dict[str, str]]) -> None:
    lines: list[str] = []
    with gzip.open(bundle_path, "rt", encoding="utf-8") as file:
        for raw_line in file:
            record = json.loads(raw_line)
            row = rows_by_id.get(record["id"])
            if row:
                record["answer"] = int(row["answer"])
            lines.append(json.dumps(record, ensure_ascii=False))

    with gzip.open(bundle_path, "wt", encoding="utf-8") as file:
        file.write("\n".join(lines))
        file.write("\n")


def sync_answers(year: str, subject: str) -> list[str]:
    config = SUBJECT_CONFIG[subject]
    csv_path = ROOT / "data" / f"{year}_{config['csv_name']}.csv"
    bundle_path = ROOT / "dist" / "bundles" / f"{year}.jsonl.gz"

    rows = load_csv_rows(csv_path)
    if len(rows) != 40:
        raise ValueError(f"{csv_path.name}: Expected 40 rows, got {len(rows)}")

    fieldnames = list(rows[0].keys())
    rows_by_no = {int(row["question_no"]): row for row in rows}
    if sorted(rows_by_no) != list(range(1, 41)):
        raise ValueError(f"{csv_path.name}: question_no must be 1..40")

    source_text = load_source_text(year, subject)
    answers = parse_answers(source_text)

    changed_ids: list[str] = []
    bundle_updates: dict[str, dict[str, str]] = {}
    for question_no, answer in enumerate(answers, start=1):
        row = rows_by_no[question_no]
        if row["answer"] != answer:
            row["answer"] = answer
            changed_ids.append(row["id"])
        bundle_updates[row["id"]] = row

    write_csv_rows(csv_path, rows, fieldnames)
    sync_bundle(bundle_path, bundle_updates)
    return changed_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", choices=sorted(YEAR_TO_SOURCE))
    parser.add_argument("--subject", choices=sorted(SUBJECT_CONFIG))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all:
        return args
    if not args.year or not args.subject:
        parser.error("Specify --all or both --year and --subject")
    return args


def main() -> None:
    args = parse_args()
    targets: list[tuple[str, str]] = []
    if args.all:
        for year in sorted(YEAR_TO_SOURCE):
            for subject in ("gyousei", "kanteihyoka"):
                targets.append((year, subject))
    else:
        targets.append((args.year, args.subject))

    for year, subject in targets:
        changed_ids = sync_answers(year, subject)
        print(f"{year} {subject}: updated {len(changed_ids)} rows")
        if changed_ids:
            print(", ".join(changed_ids))


if __name__ == "__main__":
    main()
