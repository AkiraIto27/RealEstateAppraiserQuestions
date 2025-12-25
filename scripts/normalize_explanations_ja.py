#!/usr/bin/env python3

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

DEFAULT_INPUT_DIR = "dist_with_ai/bundles"
DEFAULT_MODEL = "qwen2.5:7b-instruct"
DEFAULT_TIMEOUT = 180
DEFAULT_LOG = "rag_errors.txt"

# Simplified Chinese markers (characters/words uncommon in Japanese)
SIMPLIFIED_CHARS = set(
    "为发应规类权产现无别对过关并么吗们书业义实东线话举处还证复议没选"  # noqa: E501
)
CHINESE_WORDS = [
    "根据",
    "因此",
    "由于",
    "可以",
    "应该",
    "不得",
    "以及",
    "其中",
    "属于",
    "但是",
    "如果",
    "所以",
    "说明",
    "同时",
    "或者",
    "并且",
    "并非",
    "选项",
    "正确",
    "错误",
    "行为",
    "规定",
]


def contains_simplified_chinese(text: str) -> bool:
    if any(ch in SIMPLIFIED_CHARS for ch in text):
        return True
    for w in CHINESE_WORDS:
        if w in text:
            return True
    # Chinese comma/semicolon are also useful hints
    if "，" in text or "；" in text:
        return True
    return False


def ollama_rewrite(ollama_url: str, model: str, text: str, timeout: int) -> Optional[str]:
    system_prompt = (
        "あなたは日本語の校閲者です。\n"
        "与えられた解説文を、意味を変えずに日本語だけで書き直してください。\n"
        "中国語・英語は禁止です。\n"
        "番号・見出し・法令名などの固有表現は保持してください。\n"
        "出力はJSONのみで、キーは explanation（string）です。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "format": "json",
    }
    resp = requests.post(ollama_url.rstrip("/") + "/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    content = data.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except Exception:
        # Fallback: try to extract JSON object
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None

    explanation = parsed.get("explanation")
    if not isinstance(explanation, str):
        return None
    return explanation


def process_bundle(
    bundle_path: Path,
    output_path: Path,
    model: str,
    ollama_url: str,
    timeout: int,
    dry_run: bool,
    error_log: Optional[Path],
) -> Dict[str, int]:
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    total = 0
    modified = 0
    skipped = 0
    errors: List[str] = []

    with gzip.open(bundle_path, "rt", encoding="utf-8") as fin, gzip.open(tmp_path, "wt", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                fout.write(line + "\n")
                continue

            total += 1
            qid = obj.get("id", f"line{total}")
            explanation = obj.get("explanation")
            if not isinstance(explanation, str) or not explanation.strip():
                skipped += 1
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            if not contains_simplified_chinese(explanation):
                skipped += 1
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            try:
                rewritten = ollama_rewrite(ollama_url, model, explanation, timeout)
                if not rewritten:
                    raise ValueError("rewrite_failed")
                obj["explanation"] = rewritten
                if "updated_at" in obj:
                    obj["updated_at"] = datetime.now(timezone.utc).isoformat()
                modified += 1
            except Exception as e:
                errors.append(qid)
                print(f"[warn] id={qid} err={e}")

            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

    if dry_run:
        tmp_path.unlink(missing_ok=True)
        print(f"[dry-run] {bundle_path.name} modified={modified} skipped={skipped} total={total}")
    else:
        tmp_path.replace(output_path)
        print(f"[update] {output_path} modified={modified} skipped={skipped} total={total}")

    if error_log is not None and errors:
        with open(error_log, "a", encoding="utf-8") as f:
            for qid in sorted(set(errors)):
                f.write(qid + "\n")

    return {"total": total, "modified": modified, "skipped": skipped, "errors": len(errors)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize explanations to Japanese")
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, help="input bundles dir")
    parser.add_argument("--output-dir", default=DEFAULT_INPUT_DIR, help="output bundles dir")
    parser.add_argument("--bundle", default="", help="single bundle filename (e.g., r07.jsonl.gz)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ollama model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="ollama base url")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="ollama timeout seconds")
    parser.add_argument("--dry-run", action="store_true", help="do not write output")
    parser.add_argument("--error-log", default=DEFAULT_LOG, help="error log file path")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    if not input_dir.exists():
        raise FileNotFoundError(f"input dir not found: {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    bundle_files = sorted([p for p in input_dir.glob("*.jsonl.gz")])
    if args.bundle:
        target = args.bundle
        bundle_files = [p for p in bundle_files if p.name == target]

    if not bundle_files:
        raise FileNotFoundError("no bundle files found")

    error_log = Path(args.error_log) if args.error_log else None

    for bundle in bundle_files:
        out_path = output_dir / bundle.name
        process_bundle(
            bundle_path=bundle,
            output_path=out_path,
            model=args.model,
            ollama_url=args.ollama_url,
            timeout=args.timeout,
            dry_run=args.dry_run,
            error_log=error_log,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
