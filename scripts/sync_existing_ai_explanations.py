#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Dict, Iterable


def load_jsonl_gz(path: Path) -> list[dict]:
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl_gz(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_existing_map(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    return {row["id"]: row for row in load_jsonl_gz(path) if row.get("id")}


def sync_bundle(input_path: Path, output_path: Path) -> tuple[int, int]:
    incoming_rows = load_jsonl_gz(input_path)
    existing_by_id = build_existing_map(output_path)
    copied = 0

    for row in incoming_rows:
        previous = existing_by_id.get(row.get("id", ""))
        if not previous:
            continue
        explanation = previous.get("explanation")
        if explanation:
            row["explanation"] = explanation
            copied += 1
        if "law_citations" in previous:
            row["law_citations"] = previous["law_citations"]

    write_jsonl_gz(output_path, incoming_rows)
    return len(incoming_rows), copied


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy existing AI explanations from dist_with_ai/bundles onto refreshed dist/bundles."
    )
    parser.add_argument("--input-dir", default="dist/bundles", help="source bundles directory")
    parser.add_argument("--output-dir", default="dist_with_ai/bundles", help="target bundles directory")
    parser.add_argument("--bundle", default="", help="single bundle file name to sync")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"input dir not found: {input_dir}")

    bundle_paths = sorted(input_dir.glob("*.jsonl.gz"))
    if args.bundle:
        bundle_paths = [p for p in bundle_paths if p.name == Path(args.bundle).name]

    if not bundle_paths:
        raise FileNotFoundError("no bundle files found")

    for input_path in bundle_paths:
        output_path = output_dir / input_path.name
        total, copied = sync_bundle(input_path, output_path)
        print(f"[sync] {input_path.name} rows={total} copied_explanations={copied}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
