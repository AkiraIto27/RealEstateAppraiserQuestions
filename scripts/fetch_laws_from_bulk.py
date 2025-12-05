#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from typing import Set, List

import requests


ZIP_URL = "https://laws.e-gov.go.jp/bulkdownload?file_section=1&only_xml_flag=true"

RAW_ZIP_DIR = "laws_raw"
LAWS_DIR = "laws"
DATA_DIR = "data"  # rYY_*.csv がある場所


# ==================================
# Utility
# ==================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:"*?<>|]+', "_", name)


def shorten_law_name(full_name: str) -> str:
    """
    e-Govの LawName は「建築基準法（昭和二十五年法律第二百一号）」のように
    括弧付きのフル名称が多いので、「（」or "(" より前だけを取り出して
    topic 側とマッチさせる。
    """
    short = re.split(r"[（(]", full_name, maxsplit=1)[0]
    return short.strip()


# ==================================
# 年度計算
# ==================================

def calc_exam_and_law_years():
    today = date.today()
    exam_year = today.year        # 例: 2025
    law_year = exam_year - 1      # 例: 2024

    reiwa_year = exam_year - 2018
    if reiwa_year <= 0:
        raise ValueError("Reiwa year calculation error")

    law_cutoff_date = f"{law_year}-09-01"
    return exam_year, reiwa_year, law_cutoff_date


# ==================================
# CSV 読み込み
# ==================================

def collect_topics_from_csv(path: str) -> Set[str]:
    topics: Set[str] = set()

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "topic" not in reader.fieldnames:
            raise ValueError(f"'topic' 列がありません: {path}")

        for row in reader:
            t = (row.get("topic") or "").strip()
            if t:
                topics.add(t)

    return topics


# ==================================
# topic → 法律名抽出
# ==================================

MANUAL_TOPIC_TO_LAWS = {
    # 必要になったらここにマッピングを足していく想定
    # 例:
    # "固定資産税": ["地方税法"],
    # "不動産の表示に関する登記": ["不動産登記法"],
}

def split_compound_topic(topic: str) -> List[str]:
    """
    「金融商品取引法、投資信託及び投資法人に関する法律及び資産の流動化に関する法律」
    みたいなものをまず「、」で分ける。
    「A法及びB法」みたいなパターンは、あとで MANUAL_TOPIC_TO_LAWS で対処していく想定。
    """
    parts = re.split("、", topic)
    results = []
    for p in parts:
        p = p.strip()
        if p:
            results.append(p)
    return results


def extract_law_names_from_topics(all_topics: Set[str]) -> Set[str]:
    law_names: Set[str] = set()

    for t in all_topics:

        # 手動マッピング優先
        if t in MANUAL_TOPIC_TO_LAWS:
            for ln in MANUAL_TOPIC_TO_LAWS[t]:
                law_names.add(ln)
            continue

        # topic を分割して「法／法律」で終わるものだけ拾う
        for piece in split_compound_topic(t):
            if not (piece.endswith("法") or piece.endswith("法律")):
                continue
            law_names.add(piece)

    return law_names


# ==================================
# bulkdownload ZIP
# ==================================

def download_bulk_zip(law_cutoff_date: str) -> str:
    ensure_dir(RAW_ZIP_DIR)
    zip_path = os.path.join(RAW_ZIP_DIR, f"{law_cutoff_date}.zip")

    if os.path.exists(zip_path):
        print("bulk ZIP already exists:", zip_path)
        return zip_path

    print("Downloading bulk ZIP...")
    resp = requests.get(ZIP_URL, stream=True, timeout=120)
    resp.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    print("Saved ZIP:", zip_path)
    return zip_path


# ==================================
# XML から LawName を取り出す
# ==================================

def extract_law_name_from_xml(xml_bytes: bytes) -> str | None:
    """
    bulkdownload の法令XMLから法令名を取り出す。
    通常は <LawTitle> に入っているが、念のため LawName もフォールバックで見る。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    law_title = None
    law_name = None

    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue

        # LawTitle（法令名）を優先
        if tag.endswith("LawTitle") and el.text:
            txt = el.text.strip()
            if txt:
                law_title = txt

        # 念のため LawName も拾う（API由来XMLを扱う場合のフォールバック）
        if tag.endswith("LawName") and el.text:
            txt = el.text.strip()
            if txt:
                law_name = txt

    # LawTitle があればそれを使う
    if law_title:
        return law_title
    # なければ LawName を返す
    if law_name:
        return law_name

    return None



# ==================================
# ZIP → 抽出
# ==================================

def extract_target_laws_from_zip(
    zip_path: str,
    law_cutoff_date: str,
    target_names: Set[str],
) -> None:
    out_dir = os.path.join(LAWS_DIR, law_cutoff_date)
    ensure_dir(out_dir)

    found_full: Set[str] = set()
    found_short: Set[str] = set()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():

            if not info.filename.lower().endswith(".xml"):
                continue

            with zf.open(info) as f:
                xml_bytes = f.read()

            full_name = extract_law_name_from_xml(xml_bytes)
            if not full_name:
                continue

            short_name = shorten_law_name(full_name)

            # フル名称 or 省略名のどちらかが target_names に入っていればヒットとみなす
            if (full_name not in target_names) and (short_name not in target_names):
                continue

            found_full.add(full_name)
            found_short.add(short_name)

            out_name = f"{sanitize_filename(short_name)}_{sanitize_filename(info.filename)}"
            out_path = os.path.join(out_dir, out_name)

            with open(out_path, "wb") as out:
                out.write(xml_bytes)

            print(f"Extracted: {full_name} -> {out_path}")

    # インデックスファイルを書いて後から確認できるようにする
    index_path = os.path.join(out_dir, "_index.txt")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(f"law_cutoff_date: {law_cutoff_date}\n")

        f.write("\n[Target law names from topics]\n")
        for name in sorted(target_names):
            f.write(f"- {name}\n")

        f.write("\n[Found law full names in bulk XML]\n")
        for name in sorted(found_full):
            f.write(f"- {name}\n")

        f.write("\n[Found law short names]\n")
        for name in sorted(found_short):
            f.write(f"- {name}\n")

        missing = target_names - found_short
        f.write("\n[Missing law names (by short name)]\n")
        for name in sorted(missing):
            f.write(f"- {name}\n")

    print(f"Written index file: {index_path}")

    if missing:
        print("⚠ Some law names were not found (see _index.txt)")
    else:
        print("All target laws extracted.")


# ==================================
# メイン
# ==================================

def main():
    exam_year, reiwa_year, law_cutoff_date = calc_exam_and_law_years()

    print(f"Exam year (西暦): {exam_year}")
    print(f"Exam year (令和): {reiwa_year}")
    print(f"Law cutoff date : {law_cutoff_date}")

    yy = f"{reiwa_year:02d}"
    kantei_csv = os.path.join(DATA_DIR, f"r{yy}_kanteihyoka.csv")
    gyousei_csv = os.path.join(DATA_DIR, f"r{yy}_gyousei.csv")

    topics = collect_topics_from_csv(kantei_csv) | collect_topics_from_csv(gyousei_csv)
    print(f"Total unique topics: {len(topics)}")

    law_names = extract_law_names_from_topics(topics)
    print(f"Extracted law-like names from topics: {len(law_names)}")
    for ln in sorted(law_names):
        print(f"  - {ln}")

    if not law_names:
        print("No law-like topics found. Abort.")
        return

    zip_path = download_bulk_zip(law_cutoff_date)
    extract_target_laws_from_zip(zip_path, law_cutoff_date, law_names)


if __name__ == "__main__":
    main()
