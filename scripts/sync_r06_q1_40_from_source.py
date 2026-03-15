import csv
import gzip
import json
import re
import subprocess
from pathlib import Path


ROOT = Path("/Users/itouakira/Documents/GitHub/RealEstateAppraiserQuestions")
CSV_PATH = ROOT / "data" / "r06_gyousei.csv"
SOURCE_PATH = ROOT / "scripts" / "r06_q1_40_source.txt"
PDF_PATH = ROOT / "raw_data" / "2024" / "gyousei_question.pdf"
BUNDLE_GZ_PATH = ROOT / "dist" / "bundles" / "r06.jsonl.gz"

QUESTION_RE = re.compile(r"〔問題\s*([0-9０-９]+)〕\s*(.*)")
CHOICE_RE = re.compile(r"^(?:[⑴⑵⑶⑷⑸]|\(([1-5])\))\s*(.*)")
TOP_LEVEL_IROHA_RE = re.compile(r"^[イロハニホ][ 　]")
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_number(text: str) -> int:
    return int(text.translate(FULLWIDTH_DIGITS))


def load_source_text() -> str:
    if PDF_PATH.exists():
        text = extract_pdf_text().strip()
        SOURCE_PATH.write_text(text, encoding="utf-8")
    else:
        text = SOURCE_PATH.read_text(encoding="utf-8").strip()
    if text.startswith("「"):
        text = text[1:]
    if text.endswith("」"):
        text = text[:-1]
    return text.strip()


def extract_pdf_text() -> str:
    swift_code = f"""
import PDFKit
import Foundation

let url = URL(fileURLWithPath: "{PDF_PATH}")
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


def split_question_blocks(text: str) -> list[tuple[int, str]]:
    matches = list(QUESTION_RE.finditer(text))
    blocks: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        number = normalize_number(match.group(1))
        blocks.append((number, text[start:end].strip()))
    return blocks


def normalize_lines(block_text: str) -> list[str]:
    lines = [line.rstrip() for line in block_text.splitlines()]
    return [line for line in lines if line.strip()]


def infer_topic(first_line: str) -> str | None:
    patterns = [
        r"^(.+?)（以下この問において「法」という。）に関する次",
        r"^(.+?)に関する次",
        r"^下記の説明文は、(.+?)に関する記述である。",
        r"^交換により取得した資産の圧縮額の損金算入（(.+?)第",
        r"^特定の資産の買換えの場合の課税の特例（(.+?)第",
    ]
    for pattern in patterns:
        match = re.match(pattern, first_line)
        if match:
            return match.group(1).strip()
    return None


def format_statement(statement_lines: list[str], exam: str) -> str:
    if exam != "combo_iroha":
        return "\n".join(statement_lines)

    formatted: list[str] = []
    for index, line in enumerate(statement_lines):
        if index == 0:
            formatted.append(line)
        elif TOP_LEVEL_IROHA_RE.match(line):
            formatted.append(f"</border>{line}")
        else:
            formatted.append(line)
    return "\n".join(formatted)


def parse_block(block_text: str, exam: str) -> dict[str, str]:
    lines = normalize_lines(block_text)
    if not lines:
        raise ValueError("Empty block")

    header_match = QUESTION_RE.match(lines[0])
    if not header_match:
        raise ValueError(f"Unexpected header: {lines[0]}")

    statement_lines: list[str] = []
    header_rest = header_match.group(2).strip()
    if header_rest:
        statement_lines.append(header_rest)

    choices: list[list[str]] = []
    current_choice: list[str] | None = None

    for line in lines[1:]:
        if re.fullmatch(r"\d+", line.strip()):
            continue

        choice_match = CHOICE_RE.match(line)
        if choice_match:
            if current_choice is not None:
                choices.append(current_choice)
            current_choice = [choice_match.group(2)]
            continue

        if current_choice is None:
            statement_lines.append(line)
        else:
            current_choice.append(line)

    if current_choice is not None:
        choices.append(current_choice)

    if len(choices) != 5:
        raise ValueError(f"Expected 5 choices, got {len(choices)}")

    return {
        "statement": format_statement(statement_lines, exam),
        "choice1": "\n".join(choices[0]),
        "choice2": "\n".join(choices[1]),
        "choice3": "\n".join(choices[2]),
        "choice4": "\n".join(choices[3]),
        "choice5": "\n".join(choices[4]),
        "topic": infer_topic(statement_lines[0]) or "",
    }


def load_csv_rows() -> list[dict[str, str]]:
    with CSV_PATH.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv_rows(rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sync_bundle(rows_by_id: dict[str, dict[str, str]]) -> None:
    lines: list[str] = []
    with gzip.open(BUNDLE_GZ_PATH, "rt", encoding="utf-8") as file:
        for raw_line in file:
            record = json.loads(raw_line)
            row = rows_by_id.get(record["id"])
            if row:
                record["topic"] = row["topic"]
                record["statement"] = row["statement"]
                record["choices"] = [
                    {"key": 1, "text": row["choice1"]},
                    {"key": 2, "text": row["choice2"]},
                    {"key": 3, "text": row["choice3"]},
                    {"key": 4, "text": row["choice4"]},
                    {"key": 5, "text": row["choice5"]},
                ]
            lines.append(json.dumps(record, ensure_ascii=False))

    with gzip.open(BUNDLE_GZ_PATH, "wt", encoding="utf-8") as file:
        file.write("\n".join(lines))
        file.write("\n")


def main() -> None:
    rows = load_csv_rows()
    fieldnames = list(rows[0].keys())
    rows_by_no = {int(row["question_no"]): row for row in rows}

    source_text = load_source_text()
    changed_ids: list[str] = []
    bundle_updates: dict[str, dict[str, str]] = {}

    for number, block in split_question_blocks(source_text):
        if number < 1 or number > 40:
            continue
        row = rows_by_no[number]
        parsed = parse_block(block, row["exam"])
        row_changed = False

        for key in ("statement", "choice1", "choice2", "choice3", "choice4", "choice5"):
            if row[key] != parsed[key]:
                row[key] = parsed[key]
                row_changed = True

        if parsed["topic"] and row["topic"] != parsed["topic"]:
            row["topic"] = parsed["topic"]
            row_changed = True

        if row_changed:
            changed_ids.append(row["id"])
        bundle_updates[row["id"]] = row

    write_csv_rows(rows, fieldnames)
    sync_bundle(bundle_updates)

    print("updated:", ", ".join(changed_ids))


if __name__ == "__main__":
    main()
