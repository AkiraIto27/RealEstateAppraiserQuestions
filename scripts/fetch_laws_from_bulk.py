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


# ==================================
# 設定
# ==================================

# e-Gov bulkdownload（全法令 XML のみ）
ZIP_URL = "https://laws.e-gov.go.jp/bulkdownload?file_section=1&only_xml_flag=true"

RAW_ZIP_DIR = "laws_raw"  # 大きいZIP（Git管理しない）
LAWS_DIR = "laws"         # 抽出した法令XMLを入れる
DATA_DIR = "data"         # rYY_*.csv があるディレクトリ


# 法律っぽいけど実際は評価手法などで、法令XMLの取得対象外としたいもの
IGNORE_LIKE_LAW_TOPICS: Set[str] = {
    "DCF法",
    "収益還元法",
    "取引事例比較法",
}

# topic名 → 実際の法令名（複数可）のマッピング
# ※キーは「topic全体」だけでなく、split_compound_topic() 後の piece として
#    出てくる文字列とも一致するように書く
MANUAL_TOPIC_TO_LAWS = {
    # 「金融商品取引法、投資信託及び投資法人に関する法律及び資産の流動化に関する法律」
    # の後半部分の piece がこの文字列
    "投資信託及び投資法人に関する法律及び資産の流動化に関する法律": [
        "投資信託及び投資法人に関する法律",
        "資産の流動化に関する法律",
    ],
    # 「河川法、海岸法及び公有水面埋立法」の後半部分の piece
    "海岸法及び公有水面埋立法": [
        "海岸法",
        "公有水面埋立法",
    ],
    # topic 側の略称 → 正式名称（e-Gov の LawTitle）
    "障害者等の移動等の円滑化の促進に関する法律": [
        "高齢者、障害者等の移動等の円滑化の促進に関する法律",
    ],
}


# ==================================
# Utility
# ==================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:"*?<>|]+', "_", name)


def shorten_law_name(full_name: str) -> str:
    """
    「建築基準法（昭和二十五年法律第二百一号）」→「建築基準法」
    のように、括弧より前だけに短縮する。
    """
    short = re.split(r"[（(]", full_name, maxsplit=1)[0]
    return short.strip()


# ==================================
# 年度計算
# ==================================

def calc_exam_and_law_years():
    """
    - 試験年度: 今年（西暦）
    - 適用法令年度: 今年 - 1
    - law_cutoff_date: {適用法令年度}-09-01
    - 令和年: 試験年度 - 2018
    """
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
        if "topic" not in (reader.fieldnames or []):
            raise ValueError(f"'topic' 列がありません: {path}")

        for row in reader:
            t = (row.get("topic") or "").strip()
            if t:
                topics.add(t)

    return topics


# ==================================
# topic → 法律名抽出
# ==================================

def split_compound_topic(topic: str) -> List[str]:
    """
    「金融商品取引法、投資信託及び投資法人に関する法律及び資産の流動化に関する法律」
    などを、まず「、」で粗く分割する。
    （「A法及びB法」のようなものは MANUAL_TOPIC_TO_LAWS で補正する方針）
    """
    parts = re.split("、", topic)
    results = []
    for p in parts:
        p = p.strip()
        if p:
            results.append(p)
    return results


def extract_law_names_from_topics(all_topics: Set[str]) -> Set[str]:
    """
    topic集合から「法令として扱いたい名前」だけを抽出する。
    - IGNORE_LIKE_LAW_TOPICS はスキップ
    - MANUAL_TOPIC_TO_LAWS があればそれを優先
    - それ以外は「法」「法律」で終わるもののみ採用
    """
    law_names: Set[str] = set()

    for t in all_topics:

        # 評価手法など「法律ではないもの」はスキップ
        if t in IGNORE_LIKE_LAW_TOPICS:
            continue

        # topic 全体が MANUAL マッピング対象なら、それを採用して次へ
        if t in MANUAL_TOPIC_TO_LAWS:
            for ln in MANUAL_TOPIC_TO_LAWS[t]:
                law_names.add(ln)
            continue

        # topic を分割して piece 単位で処理
        for piece in split_compound_topic(t):

            # piece 自体が無視対象ならスキップ
            if piece in IGNORE_LIKE_LAW_TOPICS:
                continue

            # piece が MANUAL マッピング対象なら、そのマッピングを使う
            if piece in MANUAL_TOPIC_TO_LAWS:
                for ln in MANUAL_TOPIC_TO_LAWS[piece]:
                    law_names.add(ln)
                continue

            # それ以外は単純に「法／法律」で終わるものだけ拾う
            if not (piece.endswith("法") or piece.endswith("法律")):
                continue

            law_names.add(piece)

    return law_names


# ==================================
# bulkdownload ZIP
# ==================================

def download_bulk_zip(law_cutoff_date: str) -> str:
    """
    bulkdownload ZIP を laws_raw/{LAW_CUTOFF_DATE}.zip に保存
    （すでにあれば再ダウンロードしない）
    """
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
# XML から法令名を取り出す
# ==================================

def extract_law_name_from_xml(xml_bytes: bytes) -> str | None:
    """
    bulkdownload の法令XMLから法令名を取り出す。
    通常は <LawTitle> に入っている。
    念のため LawName があればそれもフォールバックで見る。
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

        if tag.endswith("LawTitle") and el.text:
            txt = el.text.strip()
            if txt:
                law_title = txt

        if tag.endswith("LawName") and el.text:
            txt = el.text.strip()
            if txt:
                law_name = txt

    if law_title:
        return law_title
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
    """
    bulk ZIP 内の XML を走査し、
    - LawTitle / LawName（full）
    - そこから切り出した short_name
    のどちらかが target_names に含まれるものだけを
    laws/{LAW_CUTOFF_DATE}/ に保存する。
    併せて _index.txt にターゲット／ヒット／Missing を出力。
    """
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

            # full_name / short_name のどちらかが target に含まれればヒット
            if (full_name not in target_names) and (short_name not in target_names):
                continue

            found_full.add(full_name)
            found_short.add(short_name)

            out_name = f"{sanitize_filename(short_name)}_{sanitize_filename(info.filename)}"
            out_path = os.path.join(out_dir, out_name)

            with open(out_path, "wb") as out:
                out.write(xml_bytes)

            print(f"Extracted: {full_name} -> {out_path}")

    # インデックスファイルで状況を記録
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
