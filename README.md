# RealEstate Appraiser Questions / Past Questions Dataset

不動産鑑定士（短答式）過去問を **年度CSV → JSONLバンドル（gzip）→ manifest.json** に変換し、
オフライン対応のモバイルアプリから取得しやすい形式で GitHub Pages で公開するためのリポジトリです。

さらに本リポジトリでは、e-Gov法令XML一括DLを元に作った **法令テキスト索引（laws_index）** を使い、
**ローカル/外部GPU対応RAG（Chroma + Ollama/OpenAI-compatible）で根拠条文に基づくAI解説を生成**して `dist_with_ai/bundles` を作成できます。

---

## 目次

- ディレクトリ構成
- データ仕様
  - CSVスキーマ
  - dist(JSONL)スキーマ
- ビルドと公開（基本）
- 法令データの取り込み（e-Gov → XML → TXT索引）
- ローカル/外部GPU対応RAG（Chroma + Ollama/OpenAI-compatible）で解説生成
- GitHub Actions（自動化フロー）
- トラブルシュート
- 出典・ライセンス

---

## ディレクトリ構成

```text
RealEstateAppraiserQuestions/
├─ data/                       # 原本（年度×科目のCSV）
├─ dist/                       # 生成物（manifest.json と bundles/.jsonl.gz）
│  ├─ manifest.json
│  └─ bundles/
│     ├─ r03.jsonl.gz
│     ├─ r04.jsonl.gz
│     └─ ...
├─ laws/
│  └─ YYYY-MM-DD/              # e-Gov由来の法令XMLスナップショット（取得日ごと）
├─ laws_index/
│  └─ YYYY-MM-DD/              # laws_xml_to_txt により生成されるTXT索引（RAG入力用）
├─ scripts/
│  ├─ build.js                 # CSV → dist生成
│  ├─ check_exam_consistency.mjs # exam分類の検査・修正
│  ├─ fetch_laws_from_bulk.py  # e-Gov一括DLから laws/YYYY-MM-DD/ へ展開（想定）
│  ├─ laws_xml_to_txt.mjs      # laws/YYYY-MM-DD/.xml → laws_index/YYYY-MM-DD/*.txt
│  ├─ rag_local.py             # ローカルRAGで解説生成
│  └─ requirements-rag.txt     # ローカルRAG依存
└─ .github/workflows/
   ├─ build.yml                # build/commit/pages
   └─ fetch_laws.yml           # e-Gov法令の取得＆索引生成
```

---

## データ仕様

### CSVスキーマ

ヘッダ（固定・21列）

```csv
id,year,era,era_year,exam,subject,topic,question_no,statement,
choice1,choice2,choice3,choice4,choice5,answer,
explanation,law_citations,difficulty,tags,source_page,updated_at
```

- 5択固定（choice1〜choice5）
- answer は 1..5 の数値
- law_citations は `;` 区切り（例：`土地基本法:第X条; 都計法:第Y条`）
- tags はカンマ区切り（例：`頻出,改正2025`）
- 文章にカンマ/改行があるセルは引用符で囲む（Excel/スプレッドシートでOK）

### exam分類

`exam` は消費側アプリの `QuestionExamPattern`
（`RealEstateAppraiser/composeApp/src/commonMain/kotlin/com/real/estate/appraiser/core/model/Question.kt`）
に対応する出題形式コードです。
問題作成・取り込み・CSV修正・バンドル再生成の前後では、必ず `npm run exam:check` を実行してください。
不整合が出た場合は `npm run exam:fix` で `data/*.csv` と既存バンドルの `exam` を修正してから再ビルドします。

```bash
npm run exam:check
npm run exam:fix
npm run build
npm run sync:ai-copy
node scripts/update_manifest.mjs --dist dist_with_ai
npm run exam:check
```

分類の仕様:

- `combo_iroha`: `イ/ロ/ハ/ニ/ホ` の記述群から、`イとロ`、`イとロとハ`、`イのみ` などの純粋な組合せを選ぶ問題。
- `single_select`: 選択肢が文章で、正しいもの・誤っているものなどを1つ選ぶ問題。
- `fill_blank`: 空欄補充・穴埋め問題。空欄ラベルに `イ/ロ/ハ` を使っていても、空欄に語句を入れる形式ならこちら。
- `calc_numeric`: 前提条件・数値に基づいて計算し、円・％・㎡などの数値結果を選ぶ問題。

### 鑑定評価39・40問の表形式

`不動産の鑑定評価に関する理論` の `question_no` 39・40 は、前提条件や数値表を使う表形式の計算問題として扱います。
この2問は、空欄ラベル（例: `ア`〜`エ`）を含む場合でも `exam=calc_numeric` に統一します。
該当行には `tags=markdown_table` を付与します。

CSVでは1レコードを1物理行に保つため、`statement` 内の表の改行は実改行ではなく `\n` エスケープで保持します。
表はMarkdown風のパイプ区切りで記述します。

```text
【前提条件】\n| 項目 | 数値 |\n|---|---|\n| 更地価格 | 50,000,000 円 |
```

JSONLでも1行1問を維持し、アプリ側で `\n` を実改行に復元したうえでMarkdown風テーブルとして描画します。
詳細は `docs/question-data-spec.md` を参照してください。

---

### dist(JSONL) スキーマ（1行=1問）

`dist/bundles/rYY.jsonl.gz` は **JSONL（1行1JSON）を gzip 圧縮**したものです。

例：

```json
{
  "id":"r05-001",
  "year":2023,
  "era":"令和",
  "era_year":5,
  "exam":"combo_iroha",
  "subject":"不動産に関する行政法規",
  "topic":"土地基本法",
  "question_no":1,
  "statement":"…",
  "choices":[
    {"key":1,"text":"…"},
    {"key":2,"text":"…"},
    {"key":3,"text":"…"},
    {"key":4,"text":"…"},
    {"key":5,"text":"…"}
  ],
  "answer":2,
  "explanation":"（AI解説または空）",
  "law_citations":[
    "土地基本法 第○条",
    "（必要なら項・号まで）"
  ],
  "difficulty":2,
  "tags":["頻出"],
  "source":{"paper":"令和5年 行政法規","page":3},
  "updated_at":"2025-11-08T00:00:00Z"
}
```

## ビルドと公開（基本）

### ローカル

```bash
npm i
npm run build
# dist/manifest.json と dist/bundles/*.jsonl.gz が生成されます
```

### GitHub Actions（基本）

data/ を更新して main に push → Actions が走り、以下を実施：

1. `scripts/build.js` で `dist/` を生成
2. `dist/` を同じブランチにコミット（履歴に残す）
3. `dist/` を GitHub Pages にデプロイ

必要権限（workflow側）：

```yaml
permissions: { contents: write, pages: write, id-token: write }
```

リポジトリ設定 → Actions → General → Workflow permissions を Read and write に。

### 差分更新の仕組み

`manifest.json` には各年度バンドルの sha256 / etag / updated_at を格納します。
クライアントは起動時に manifest.json を取得し、差分がある年度だけ `rYY.jsonl.gz` を再DLできます。

### 動作確認（curl）

```bash
# manifest 取得
curl -sS --compressed https://<USER>.github.io/<REPO>/manifest.json | jq .

# ETag で 304 確認
ETAG=$(curl -sI https://<USER>.github.io/<REPO>/manifest.json | awk -F': ' '/^etag/i{print $2}')
curl -i -H "If-None-Match: $ETAG" https://<USER>.github.io/<REPO>/manifest.json

# バンドル取得 & 先頭レコード表示
curl -sS -o r05.jsonl.gz https://<USER>.github.io/<REPO>/bundles/r05.jsonl.gz
gunzip -c r05.jsonl.gz | head -n 1 | jq .
```

## 法令データの取り込み（e-Gov → XML → TXT索引）

### 目的

法令XML（e-Gov一括DL）をそのままLLMに投入するのではなく、**TXT化した索引（laws_index）** を作り、
ローカルRAGの検索対象にします（巨大トークン投入を避ける）。

### 手順（ローカル実行例）

```bash
# 1) laws/YYYY-MM-DD/ に XML を取得（fetch_laws_from_bulk.py 等で）
python scripts/fetch_laws_from_bulk.py

# 2) XML -> TXT索引
node scripts/laws_xml_to_txt.mjs
# => laws_index/YYYY-MM-DD/**/*.txt が生成される
```

## ローカル/外部GPU対応RAG（Chroma + Ollama/OpenAI-compatible）で解説生成

`dist/bundles/*.jsonl.gz` を入力として、根拠条文に基づく `explanation` / `law_citations` を `dist_with_ai/bundles` に生成する手順です。
Embedding は `cl-nagoya/ruri-v3-310m` を前提にし、生成 backend は以下の2系統に対応します。

- ローカル: Ollama (`--backend ollama`)
- 外部GPU推奨: OpenAI-compatible API (`--backend openai`) で vLLM を利用

既定値は後方互換のため Ollama ですが、高品質優先で外部GPUを使う場合は **vLLM + Qwen3.5-27B** を第一候補にしてください。
生成時は内部的に「正解番号・各選択肢の正誤・理由」を構造化JSONで生成し、その結果から最終的な prose を組み立てます。
`answer` と整合しない、選択肢 1〜5 の説明が欠ける、生成後レビューで不整合が見つかる場合は自動再生成されます。

### 前提

- macOS（Apple Silicon推奨、16GBメモリでも動作可）
- Python 3.10+（venvを使用）
- Node.js（XML→TXT索引の更新用）
- Homebrew（Ollama導入時のみ）

### 1) dist/bundles の存在確認（入力元）

まず JSONL が存在することを確認します。

```bash
ls dist/bundles
```

`r03.jsonl.gz` などが表示されればOKです。解説の出力先は `dist_with_ai/bundles` になります。

### 2) XML → TXT索引（laws_index）の最新化

`laws/2024-09-01` のXMLから `laws_index/2024-09-01` を生成します。
（既に生成済みならスキップしてOK）

```bash
node scripts/laws_xml_to_txt.mjs 2024-09-01
```

### 3) Python環境の準備

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements-rag.txt
```

> `sentencepiece` が足りないと言われた場合は  
> `pip install sentencepiece` を追加で実行してください。

### 4) 生成 backend の準備

#### 4-a) ローカル Ollama を使う場合

```bash
brew install ollama
brew services start ollama
```

モデル取得:

```bash
ollama pull qwen2.5:7b-instruct
```

#### 4-b) 外部GPU上の vLLM を使う場合（推奨）

ローカルMacから外部GPU API を叩きます。`rag_local.py` 側は OpenAI-compatible API を利用するため、
Runpod 等で vLLM を立て、`/v1/chat/completions` を公開してください。

```bash
export RAG_LLM_BACKEND=openai
export RAG_LLM_BASE_URL=http://<vllm-host>:8000/v1
export RAG_LLM_MODEL=Qwen/Qwen3.5-27B
# 認証がある場合のみ
export RAG_LLM_API_KEY=<token>
```

### 5) Chromaインデックス作成（RAGの土台）

`laws_index/2024-09-01` を埋め込み化してローカルに保存します。

```bash
python scripts/rag_local.py index --date 2024-09-01 --max-chars 1200 --batch-size 16 --force --log-every 1
```

無反応に見える場合は処理中です。重い場合は `--batch-size 4` に下げてください。

### 6) 解説の試験生成（まずは少数）

品質重視の目安:

- `explain`（新規生成）: `qwen2.5:14b` を優先
- `verify`（既存 explanation の厳密検証）: `qwen2.5:7b-instruct` を優先

`verify` は回答整合性・citation 整合性・条番号/項番号・文言強度のズレ検出が主目的なので、
ローカル Ollama では 14B より 7B のほうが実用的です。7B のほうが通常は速く、fail 候補の絞り込みに向いています。

ローカル Ollama:

```bash
python scripts/rag_local.py explain --bundle r07.jsonl.gz --limit 5 --log-per-question
```

外部GPU + vLLM（推奨）:

```bash
python scripts/rag_local.py explain \
  --backend openai \
  --bundle r07.jsonl.gz \
  --limit 5 \
  --timeout 300 \
  --thinking-mode off \
  --topic-filter-mode hybrid \
  --max-regenerations 3 \
  --log-per-question
```

生成結果は `dist_with_ai/bundles` に書き込まれます（入力は `dist/bundles`）。
検証結果は `dist_with_ai/verification/*.verification.jsonl` に出力されます。

### 7) 既存 explanation の厳密検証

既に `dist_with_ai/bundles` に explanation が入っている場合、まずは全件を再生成せずに `verify` を回し、
NG になった ID だけを再生成する運用を推奨します。

まずは少数確認:

```bash
python scripts/rag_local.py verify \
  --bundles dist_with_ai/bundles \
  --bundle r07.jsonl.gz \
  --limit 10 \
  --backend ollama \
  --llm-model qwen2.5:7b-instruct \
  --timeout 300 \
  --thinking-mode off \
  --topic-filter-mode hybrid \
  --log-per-question \
  --verification-report-dir dist_with_ai/verification \
  --failed-ids-file dist_with_ai/verification/failed_ids.txt
```

verify 結果は `dist_with_ai/verification/*.verify_existing.jsonl` に出力され、
fail した ID は `dist_with_ai/verification/failed_ids.txt` に保存されます。

### 8) 全量生成（dist_with_ai/bundles を上書き）

```bash
python scripts/rag_local.py explain
```

`explanation` が空の問題だけを埋めます。上書きしたい場合は `--force` を付けてください。

### 9) backend 切替・タイムアウト・リトライ

CLI 引数で backend / model / base_url / api_key を直接指定することもできます。

```bash
python scripts/rag_local.py explain \
  --backend openai \
  --base-url http://<vllm-host>:8000/v1 \
  --api-key <token> \
  --llm-model Qwen/Qwen3.5-27B
```

Ollamaが遅い場合はタイムアウトを延ばします。

```bash
python scripts/rag_local.py explain --timeout 300
```

`qwen2.5:14b` で `explain` や `verify` の LLM review を行う場合、`--timeout 300` でも足りないことがあります。
ローカル Ollama では、必要に応じて `--timeout 600` 以上を検討してください。

OpenAI-compatible backend で JSON 出力や一時的な失敗がある場合は、リトライを増やせます。

```bash
python scripts/rag_local.py explain --backend openai --timeout 300 --retries 4 --retry-backoff 3
```

生成内容の整合性チェックまで含めて厳しめに回したい場合:

```bash
python scripts/rag_local.py explain --backend openai --max-regenerations 3
```

速度優先で、生成後の LLM レビューを省略したい場合:

```bash
python scripts/rag_local.py explain --backend openai --no-llm-review
```

さらに軽くする場合:

```bash
python scripts/rag_local.py explain --timeout 300 --max-results 4 --max-context-chars 8000
```

Qwen3.5 系を vLLM で使う場合、長い思考出力で JSON が不安定になるなら `--thinking-mode off` を推奨します。

### 10) 失敗したIDだけ再実行

JSONエラー等が出たIDだけを再実行できます。`explain` 実行時は、デフォルトで `rag_errors.txt` にエラーIDが自動追記されます（不要なら `--no-error-log`）。

1) エラーIDが自動でログに溜まる（通常は何もしなくてOK）:

```bash
python scripts/rag_local.py explain
```

2) エラーIDだけ再実行:

```bash
python scripts/rag_local.py explain --ids-file rag_errors.txt
```

3) 既に埋まっているIDを上書きしたい場合:

```bash
python scripts/rag_local.py explain --ids-file rag_errors.txt --force
```

4) 直接IDを指定して再実行したい場合:

```bash
python scripts/rag_local.py explain --only-ids r07-012,r07-022,r07-048 --force
```

`verify` で fail した ID だけを再生成する場合:

```bash
python scripts/rag_local.py explain \
  --ids-file dist_with_ai/verification/failed_ids.txt \
  --force \
  --backend ollama \
  --llm-model qwen2.5:14b \
  --timeout 600 \
  --thinking-mode off \
  --topic-filter-mode hybrid \
  --max-regenerations 3 \
  --log-per-question
```

ログをクリアしたい場合は `> rag_errors.txt` で空にしてください。

### 11) 対話（RAGチャット）で確認したい場合

```bash
python scripts/rag_local.py chat --topic 土地基本法
```

外部GPU backend の確認例:

```bash
python scripts/rag_local.py chat --backend openai --topic 土地基本法 --thinking-mode off
```

### 11) 作業を終える（Python/venv/Ollamaの終了）

```bash
deactivate
```

- 通常の `python scripts/...` は完了すると自動終了します
- 対話モード（REPL）は `exit()` か `Ctrl-D` で終了
- `ollama serve` を手動起動している場合は `Ctrl-C` で終了（サービス起動なら `brew services stop ollama`）

### 参考: スクリプトの場所

- ローカルRAG本体: `scripts/rag_local.py`
- 依存パッケージ: `scripts/requirements-rag.txt`
- 主要LLM環境変数: `RAG_LLM_BACKEND`, `RAG_LLM_BASE_URL`, `RAG_LLM_MODEL`, `RAG_LLM_API_KEY`
- 検証レポート出力先: `dist_with_ai/verification/*.verification.jsonl`

## GitHub Actions（自動化フロー）

### 概要：年間スケジュール

本リポジトリは、以下のスケジュールで完全自動運用されるように設計されています。

| 時期 | トリガー | 処理概要 |
| :--- | :--- | :--- |
| **6月1日** | `Fetch Questions` (Schedule) | 最新年度の**試験問題PDF**を自動取得・CSV化してコミットします。 |
| **6/1 (直後)** | `Build` (Workflow Run) | 問題追加を受け、`dist/` を生成して公開します。 |
| **9月1日** | `Fetch Laws` (Schedule) | 最新の**法令XML**を自動取得・コミットし、`laws_index` を更新します。 |
| **9/1 (直後)** | `Build` (Workflow Run) | 法令更新を受け、`dist/` を再生成して公開します。 |

---

### 1) Secrets

GitHub Secrets に `GEMINI_API_KEY` を登録してください（`fetch_questions.yml` のPDF→CSV変換で使用）。

### 2) fetch_laws.yml（法令更新）

**実行日**: 毎年9月1日

1. e-Gov一括DL → `laws/YYYY-MM-DD/` に XML 保存
2. `laws_xml_to_txt.mjs` で `laws_index/YYYY-MM-DD/` を生成
3. 結果をコミット＆push → **完了後、自動的に `build.yml` が起動**

### 3) fetch_questions.yml（問題取得）

**実行日**: 毎年6月1日（試験日・公開日に合わせて調整可）

1. PDF取得・CSV変換
2. 結果をコミット＆push → **完了後、自動的に `build.yml` が起動**

### 4) build.yml（CSV→dist生成 + Pages公開）

**実行タイミング**:
* `main` への push
* 上記2つのFetchワークフロー完了時 (`workflow_run`)

**処理内容**:
* `scripts/build.js` で `dist/` を生成
* `dist/` をコミットして GitHub Pages にデプロイ

## トラブルシュート

### gzip/stream系エラー
`.jsonl.gz` の入出力で pipe 方向が不正だと落ちます
→ Readable -> Transform(gzip) -> Writable の順になっているか確認

### 巨大ファイル運用（laws/laws_index）
`laws/`（XML）や `laws_index/`（TXT）は大きくなります
→ 運用ポリシー（コミット対象/保持期間/スナップショット数）を決めるのがおすすめです

## 出典・ライセンス

### 不動産鑑定士試験 過去問題

本リポジトリに収録している不動産鑑定士試験の問題文および正解は、国土交通省が公開している不動産鑑定士試験問題をもとに作成しています。

- 出典：[国土交通省「不動産鑑定士試験 試験結果情報」](https://www.mlit.go.jp/totikensangyo/kanteishi/shiken02.html)
- 利用条件：[国土交通省「リンク・著作権・免責事項」](https://www.mlit.go.jp/link.html)、[公共データ利用規約（第1.0版）（PDL1.0）](https://www.digital.go.jp/resources/open_data/public_data_license_v1.0)

国土交通省ウェブサイトで特記または別の権利表記がないコンテンツは、PDL1.0に準拠した利用条件のもとで利用できます。本リポジトリでは、国土交通省が公開する試験問題をCSV / JSON / JSONL等の機械可読形式へ変換しています。問題文については原則として内容を変更せず、本リポジトリの作成者がデータ形式および構造を加工しています。

利用にあたっては、PDL1.0、国土交通省ウェブサイトの利用ルール、個別の権利表示および第三者の権利に関する条件をご確認ください。本リポジトリは、国土交通省が運営または提供するものではありません。

### 法令データ

法令データは、デジタル庁が提供するe-Gov法令検索の法令データをもとに、検索・RAG処理用としてXMLからTXT等へ変換・索引化しています。

- 出典：[e-Gov法令検索](https://laws.e-gov.go.jp/)
- データ取得元：[e-Gov法令検索「XML一括ダウンロード」](https://laws.e-gov.go.jp/bulkdownload/)
- 利用条件：[e-Gov法令検索「利用規約」](https://laws.e-gov.go.jp/terms/)、[公共データ利用規約（第1.0版）（PDL1.0）](https://www.digital.go.jp/resources/open_data/public_data_license_v1.0)

本リポジトリの作成者が加工した法令データであり、正確性・完全性・最新性を保証するものではありません。正式な法令本文については、e-Gov法令検索等の公的情報をご確認ください。
