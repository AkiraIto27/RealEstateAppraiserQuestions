# R03-R07 Question Sync Handoff

## Current Status

- `r03` is synced from the original PDFs under `raw_data/2021/`.
- Updated files:
  - `data/r03_gyousei.csv`
  - `data/r03_kanteihyoka.csv`
  - `dist/bundles/r03.jsonl.gz`
- Helper scripts added:
  - `scripts/sync_r03_q1_40_from_source.py`
  - `scripts/sync_r03_q41_80_from_source.py`
- Cached extracted source text:
  - `scripts/r03_q1_40_source.txt`
  - `scripts/r03_q41_80_source.txt`

## Source Mapping

- `raw_data/<year>/gyousei_question.pdf`
  - Questions 1-40
  - CSV target: `data/rXX_gyousei.csv`
- `raw_data/<year>/kantei_question.pdf`
  - Questions 41-80 conceptually
  - CSV target: `data/rXX_kanteihyoka.csv`
  - Important: in `rXX_kanteihyoka.csv`, `question_no` is `1-40`, while `id` is `rXX-041` to `rXX-080`

## How The Scripts Work

- The scripts use macOS `PDFKit` via `swift` to extract text directly from the source PDF.
- The extracted text is also written to `scripts/r03_q1_40_source.txt` or `scripts/r03_q41_80_source.txt` for inspection.
- Each script:
  - parses question blocks from the PDF text
  - preserves line breaks in `statement` and choices
  - updates only `topic`, `statement`, and `choice1` to `choice5`
  - syncs the matching entries in `dist/bundles/r03.jsonl.gz`

## Commands Used For R03

- First half:

```bash
python3 scripts/sync_r03_q1_40_from_source.py
```

- Second half:

```bash
python3 scripts/sync_r03_q41_80_from_source.py
```

## Known Parsing Rules

- `combo_iroha` questions are stored with `\n</border>` before top-level `イ/ロ/ハ/ニ/ホ`.
- Non-`combo_iroha` questions keep plain `\n`.
- In `kantei_question.pdf`, question 36 contains a diagram-like layout. The script intentionally keeps the last 5 detected choices for non-`combo_iroha` questions when extra `⑴`〜`⑸` markers appear earlier in the extracted text.

## Remaining Work

- `r04` sync from `raw_data/2022/`
- `r05` sync from `raw_data/2023/`
- `r06` sync from `raw_data/2024/`
- `r07` sync from `raw_data/2025/`

## Recommended Next Steps

1. Copy `scripts/sync_r03_q1_40_from_source.py` to year-specific variants for `r04` to `r07`, changing:
   - `CSV_PATH`
   - `SOURCE_PATH`
   - `PDF_PATH`
2. Copy `scripts/sync_r03_q41_80_from_source.py` to year-specific variants for `r04` to `r07`, changing:
   - `CSV_PATH`
   - `SOURCE_PATH`
   - `PDF_PATH`
3. Run each script and verify representative questions from:
   - the beginning
   - a mid-point question with line wraps
   - the end
4. Confirm `dist/bundles/rXX.jsonl.gz` matches the updated CSVs.

## Verification Checklist

- `data/rXX_gyousei.csv` has 40 rows with ids `rXX-001` to `rXX-040`
- `data/rXX_kanteihyoka.csv` has 40 rows with ids `rXX-041` to `rXX-080`
- Statements preserve PDF line breaks
- Choices preserve wrapped lines where present
- `topic` is not accidentally replaced with a wrong subject
- `dist/bundles/rXX.jsonl.gz` is updated in sync with CSV changes

## Notes

- The source text snapshots are generated artifacts for inspection and reruns.
- `explanation`, `law_citations`, `answer`, and other non-text fields were not rewritten by these sync scripts.
