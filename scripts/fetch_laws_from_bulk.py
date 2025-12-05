# scripts/fetch_laws_from_bulk.py

import os
import re
import csv
import zipfile
import xml.etree.ElementTree as ET
from datetime import date

import requests

# ========= 設定 =========

ZIP_URL   = "https://laws.e-gov.go.jp/bulkdownload?file_section=1&only_xml_flag=true"
RAW_ZIP_DIR = "laws_raw"
LAWS_DIR    = "laws"
DATA_DIR    = "data"      # rYY_*.csv がある場所


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:"*?<>|]+', "_", name)


# ========= 年度計算 =========

def calc_exam_and_law_years() -> tuple[int, int, str]:
    today = date.today()
    exam_year = today.year
    law_year = exam_year - 1

    reiwa_year = exam_year - 2018
    if reiwa_year <= 0:
        raise ValueError("Reiwa year is not positive. System date looks wrong.")

    law_cutoff_date = f"{law_year}-09-01"
    return exam_year, reiwa_year, law_cutoff_date


# ========= CSV → topic 抽出 =========

def collect_topics_from_csv(path: str) -> set[str]:
    topics: set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "topic" not in (reader.fieldnames or []):
            raise ValueError(f"'topic' 列が CSV に存在しません: {path}")
        for row in reader:
            t = (row.get("topic") or "").strip()
            if t:
                topics.add(t)
    return topics


def build_target_law_names(reiwa_year: int) -> set[str]:
    yy = f"{reiwa_year:02d}"
    kantei_path  = os.path.join(DATA_DIR, f"r{yy}_kanteihyoka.csv")
    gyousei_path = os.path.join(DATA_DIR, f"r{yy}_gyousei.csv")

    if not os.path.exists(kantei_path):
        raise FileNotFoundError(f"CSV not found: {kantei_path}")
    if not os.path.exists(gyousei_path):
        raise FileNotFoundError(f"CSV not found: {gyousei_path}")

    print(f"Reading topics from: {kantei_path}")
    topics1 = collect_topics_from_csv(kantei_path)
    print(f"  {len(topics1)} topics")

    print(f"Reading topics from: {gyousei_path}")
    topics2 = collect_topics_from_csv(gyousei_path)
    print(f"  {len(topics2)} topics")

    all_topics = topics1 | topics2
    print(f"Total unique topics (topics as-is): {len(all_topics)}")

    return all_topics


# ========= bulkdownload ZIP =========

def download_bulk_zip(law_cutoff_date: str) -> str:
    ensure_dir(RAW_ZIP_DIR)
    zip_path = os.path.join(RAW_ZIP_DIR, f"{law_cutoff_date}.zip")
    if os.path.exists(zip_path):
        print("Bulk ZIP already exists:", zip_path)
        return zip_path

    print("Downloading bulk XML ZIP from e-Gov...")
    resp = requests.get(ZIP_URL, stream=True, timeout=120)
    resp.raise_for_status()
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    print("Saved bulk ZIP:", zip_path)
    return zip_path


def extract_law_name_from_xml(xml_bytes: bytes) -> str | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    el = root.find(".//LawName")
    if el is None:
        return None
    name = (el.text or "").strip()
    return name or None


def extract_target_laws_from_zip(zip_path: str, law_cutoff_date: str, target_names: set[str]) -> None:
    out_dir = os.path.join(LAWS_DIR, law_cutoff_date)
    ensure_dir(out_dir)

    found_names: set[str] = set()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".xml"):
                continue

            with zf.open(info, "r") as f:
                xml_bytes = f.read()

            law_name = extract_law_name_from_xml(xml_bytes)
            if not law_name:
                continue

            if law_name not in target_names:
                continue

            found_names.add(law_name)

            out_name = f"{sanitize_filename(law_name)}_{sanitize_filename(info.filename)}"
            out_path = os.path.join(out_dir, out_name)
            with open(out_path, "wb") as out:
                out.write(xml_bytes)

            print(f"Extracted: {law_name} -> {out_path}")

    missing = target_names - found_names
    if missing:
        print("⚠ 以下の topic 名に対応する <LawName> が見つかりませんでした (law XML側の正式名称とズレている可能性):")
        for m in sorted(missing):
            print("  -", m)
    else:
        print("All target laws found.")


def main():
    exam_year, reiwa_year, law_cutoff_date = calc_exam_and_law_years()
    print(f"Exam year (西暦): {exam_year}")
    print(f"Exam year (令和): {reiwa_year}")
    print(f"Law cutoff date : {law_cutoff_date}  (＝ {exam_year-1} 年 9/1 時点)")

    target_law_names = build_target_law_names(reiwa_year)
    if not target_law_names:
        print("No topics found. Abort.")
        return

    zip_path = download_bulk_zip(law_cutoff_date)
    extract_target_laws_from_zip(zip_path, law_cutoff_date, target_law_names)


if __name__ == "__main__":
    main()
