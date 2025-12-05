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


def sanitize_filename(name: str# scripts/fetch_laws_from_bulk.py

import os
import re
import csv
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from typing import Set, List

import requests

# ========= 設定 =========

ZIP_URL     = "https://laws.e-gov.go.jp/bulkdownload?file_section=1&only_xml_flag=true"
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
    exam_year = today.year        # 例: 2025
    law_year = exam_year - 1      # 例: 2024

    reiwa_year = exam_year - 2018
    if reiwa_year <= 0:
        raise ValueError("Reiwa year is not positive. System date looks wrong.")

    law_cutoff_date = f"{law_year}-09-01"
    return exam_year, reiwa_year, law_cutoff_date


# ========= CSV → topic 抽出 =========

def collect_topics_from_csv(path: str) -> Set[str]:
    topics: Set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "topic" not in (reader.fieldnames or []):
            raise ValueError(f"'topic' 列が CSV に存在しません: {path}")
        for row in reader:
            t = (row.get("topic") or "").strip()
            if t:
                topics.add(t)
    return topics


# ========= topic から「法律名だけ」を取り出す =========

# 必要に応じて「topic名 → 実際の法令名」の手動マッピングを追加
MANUAL_TOPIC_TO_LAWS = {
    # 例: "固定資産税": ["地方税法"],
    # "不動産の表示に関する登記": ["不動産登記法"],
}

def split_compound_topic(topic: str) -> List[str]:
    """
    「金融商品取引法、投資信託及び投資法人に関する法律及び資産の流動化に関する法律」
    のような複合topicを「、」「及び」で粗く分割する。
    """
    # まず全角の読点で区切る
    parts = re.split("、", topic)
    results: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 「A及びBに関する法律」のようなケースもあるので、
        # 単純な split ではなく、末尾「法」「法律」を基準に拾う。
        # ここでは一旦そのまま返す（必要ならさらに改良）。
        results.append(p)
    return results


def extract_law_names_from_topics(all_topics: Set[str]) -> Set[str]:
    """
    topic の集合から「実際に法令として存在しそうな名前」だけを取り出す。
    - 末尾が「法」「法律」のもの
    - MANUAL_TOPIC_TO_LAWS に定義したもの
    """
    law_names: Set[str] = set()

    for t in all_topics:
        # 手動マッピングがあるものを優先
        if t in MANUAL_TOPIC_TO_LAWS:
            for ln in MANUAL_TOPIC_TO_LAWS[t]:
                law_names.add(ln)
            continue

        # 複合 topic を分割
        for piece in split_compound_topic(t):
            # 末尾が「法」または「法律」でないものはスキップ（評価手法や概念など）
            if not (piece.endswith("法") or piece.endswith("法律")):
                continue
            law_names.add(piece)

    return law_names


def build_target_law_names(reiwa_year: int) -> Set[str]:
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
    print(f"Total unique topics: {len(all_topics)}")

    law_names = extract_law_names_from_topics(all_topics)
    print(f"Extracted law-like names from topics: {len(law_names)}")
    for ln in sorted(law_names):
        print(f"  - {ln}")

    return law_names


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
    """
    XML から LawName を取得（namespace があっても末尾名で判断）
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    for el in root.iter():
        # el.tag が "{ns}LawName" の形式でも末尾 "LawName" で判定
        if isinstance(el.tag, str) and el.tag.endswith("LawName"):
            text = (el.text or "").strip()
            if text:
                return text
    return None


def extract_target_laws_from_zip(zip_path: str, law_cutoff_date: str, target_names: Set[str]) -> None:
    out_dir = os.path.join(LAWS_DIR, law_cutoff_date)
    ensure_dir(out_dir)

    found_names: Set[str] = set()

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
        print("⚠ 以下の法令名は bulk XML から見つかりませんでした（名前ズレ or 未収録の可能性）:")
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
        print("No law-like topics found. Abort.")
        return

    zip_path = download_bulk_zip(law_cutoff_date)
    extract_target_laws_from_zip(zip_path, law_cutoff_date, target_law_names)


if __name__ == "__main__":
    main()
) -> str:
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
