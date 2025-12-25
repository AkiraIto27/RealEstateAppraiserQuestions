# RealEstate Appraiser Questions / Past Questions Dataset

不動産鑑定士（短答式）過去問を **年度CSV → JSONLバンドル（gzip）→ manifest.json** に変換し、
オフライン対応のモバイルアプリから取得しやすい形式で GitHub Pages で公開するためのリポジトリです。

さらに本リポジトリでは、e-Gov法令XML一括DLを元に作った **法令テキスト索引（laws_index）** を OpenAI Vector Store に同期し、
**RAG（file_search）で根拠条文に基づくAI解説（explanation）を自動生成**して `dist/bundles/*.jsonl.gz` を更新できます。

---

## 目次

- ディレクトリ構成
- データ仕様
  - CSVスキーマ
  - dist(JSONL)スキーマ
- ビルドと公開（基本）
- 法令データの取り込み（e-Gov → XML → TXT索引）
- Vector Store 同期（laws_index → OpenAI Vector Store）
- AI解説生成（dist/bundles → explanation埋め）
- GitHub Actions（自動化フロー）
- トラブルシュート
- ライセンス/出典について

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
│  ├─ fetch_laws_from_bulk.py  # e-Gov一括DLから laws/YYYY-MM-DD/ へ展開（想定）
│  ├─ laws_xml_to_txt.mjs      # laws/YYYY-MM-DD/.xml → laws_index/YYYY-MM-DD/*.txt
│  ├─ openai_sync_vector_store.mjs # laws_index を OpenAI Vector Store に同期
│  └─ generate_explanations.mjs    # dist/bundles の explanation をRAGで生成して上書き
├─ .openai/                    # OpenAI同期状態（自動生成・コミット推奨）
│  ├─ vector_store_id.txt
│  └─ vector_store_sync_state.json
└─ .github/workflows/
   ├─ build.yml                # build/commit/pages +（任意）AI解説生成
   └─ fetch_laws.yml           # e-Gov法令の取得＆索引生成（＋任意でVS同期）
```

> **重要**
> GitHub Actions は毎回クリーン環境で動くため、`.openai/vector_store_id.txt` をリポジトリに残さないと
> 次回以降に Vector Store を“作り直し”になり得ます。`.openai/` はコミット推奨です。

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
  "exam":"不動産鑑定士 短答",
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
Vector Store に登録して `file_search` で参照します（巨大トークン投入を避ける）。

### 手順（ローカル実行例）

```bash
# 1) laws/YYYY-MM-DD/ に XML を取得（fetch_laws_from_bulk.py 等で）
python scripts/fetch_laws_from_bulk.py

# 2) XML -> TXT索引
node scripts/laws_xml_to_txt.mjs
# => laws_index/YYYY-MM-DD/**/*.txt が生成される
```

## Vector Store 同期（laws_index → OpenAI Vector Store）

### 必要なもの

OpenAI API Key（後述の GitHub Secrets でも可）

### 初回同期（Vector Store が無ければ自動作成）

```bash
export OPENAI_API_KEY="..."
node scripts/openai_sync_vector_store.mjs
```

- `laws_index/` 配下の **最新日付フォルダ（YYYY-MM-DD）** を自動選択して同期します
- 初回は Vector Store を作成し、`.openai/vector_store_id.txt` に `vs_...` を保存します
- 差分同期用に `.openai/vector_store_sync_state.json` も更新します

### 特定日付を同期したい

```bash
node scripts/openai_sync_vector_store.mjs --date 2024-09-01
```

### オプション

- `--dry-run`：変更検出のみでアップロードしない
- `--prune`：ローカルに無いTXTを Vector Store から外し、Filesも削除（運用注意）

### 運用のコツ

`.openai/` はコミットしておくと、Actions実行時にVS IDが引き継がれます。

## AI解説生成（dist/bundles → explanation埋め）

### 何が更新される？

`generate_explanations.mjs` は `dist/bundles/*.jsonl.gz` を読み取り、

1. `explanation` が空のレコードだけ生成（埋まっているものは skip）
2. 一時ファイル（.tmp）に書き出して最後に置換
3. 生成結果として `explanation` と `law_citations` を更新

という動きをします。
つまり “5問生成するたびに dist/bundles/*.jsonl.gz が更新されていく” イメージです。

### ローカル実行例

```bash
export OPENAI_API_KEY="..."
# 先に openai_sync_vector_store.mjs を実行して .openai/vector_store_id.txt を作っておく
node scripts/generate_explanations.mjs --model gpt-5-mini --limit 5
```

### 重要：RAGで根拠縛り

このスクリプトは Responses API の `tools: file_search` を有効化し、vector_store_ids に作成済みのVSを指定して検索させます。
プロンプトで次を強制します：

- 検索で根拠が取れない内容は推測しない
- 根拠条文を特定できない場合は、`law_citations=[]` にして `explanation` へ「保留」等を明記

### 主なオプション

- `--model`：使用モデル（例：gpt-5-mini, gpt-5.2, gpt-4o-mini など）
- `--temperature`：モデルによっては非対応（例：一部のGPT-5系）。非対応モデルでは指定を外してください
- `--max-results 8`：file_search の最大取得件数（小さくすると入力トークン節約）
- `--limit 5`：最大処理件数（テスト/チェックポイント用）
- `--dry-run`：生成はするがファイルを書き換えない

## ローカルRAG（Chroma + Ollama）で解説生成

OpenAI API を使わず、ローカル環境だけで `dist/bundles/*.jsonl.gz` の `explanation` を埋める手順です。
Embedding は `cl-nagoya/ruri-v3-310m`、生成 LLM は Ollama の `qwen2.5:7b-instruct` を前提にしています。

### 前提

- macOS（Apple Silicon推奨、16GBメモリでも動作可）
- Python 3.10+（venvを使用）
- Node.js（XML→TXT索引の更新用）
- Homebrew（Ollama導入に使う場合）

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

### 4) Ollamaのインストールと起動

```bash
brew install ollama
brew services start ollama
```

モデル取得:

```bash
ollama pull qwen2.5:7b-instruct
```

### 5) Chromaインデックス作成（RAGの土台）

`laws_index/2024-09-01` を埋め込み化してローカルに保存します。

```bash
python scripts/rag_local.py index --date 2024-09-01 --max-chars 1200 --batch-size 16 --force --log-every 1
```

無反応に見える場合は処理中です。重い場合は `--batch-size 4` に下げてください。

### 6) 解説の試験生成（まずは少数）

```bash
python scripts/rag_local.py explain --bundle r07.jsonl.gz --limit 5 --log-per-question
```

生成結果は `dist_with_ai/bundles` に書き込まれます（入力は `dist/bundles`）。

### 7) 全量生成（dist_with_ai/bundles を上書き）

```bash
python scripts/rag_local.py explain
```

`explanation` が空の問題だけを埋めます。上書きしたい場合は `--force` を付けてください。

### 8) タイムアウト対策

Ollamaが遅い場合はタイムアウトを延ばします。

```bash
python scripts/rag_local.py explain --timeout 300
```

さらに軽くする場合:

```bash
python scripts/rag_local.py explain --timeout 300 --max-results 4 --max-context-chars 8000
```

### 9) 失敗したIDだけ再実行

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

ログをクリアしたい場合は `> rag_errors.txt` で空にしてください。

### 10) 対話（RAGチャット）で確認したい場合

```bash
python scripts/rag_local.py chat --topic 土地基本法
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

## GitHub Actions（自動化フロー）

### 概要：年間スケジュール

本リポジトリは、以下のスケジュールで完全自動運用されるように設計されています。

| 時期 | トリガー | 処理概要 |
| :--- | :--- | :--- |
| **6月1日** | `Fetch Questions` (Schedule) | 最新年度の**試験問題PDF**を自動取得・CSV化してコミットします。 |
| **6/1 (直後)** | `Build` (Workflow Run) | 問題追加を受け、**新規問題のみ**AI解説を生成して公開します。 |
| **9月1日** | `Fetch Laws` (Schedule) | 最新の**法令XML**を自動取得・コミットして、Vector Storeを更新します。 |
| **9/1 (直後)** | `Build` (Workflow Run) | 法令更新を受け、**全年度の解説を新法令に基づいて強制再生成**します。 |

---

### 1) Secrets

GitHub Secrets に `OPENAI_API_KEY` を登録してください（ActionsからOpenAI APIを叩くため）。

### 2) fetch_laws.yml（法令更新）

**実行日**: 毎年9月1日

1. e-Gov一括DL → `laws/YYYY-MM-DD/` に XML 保存
2. `laws_xml_to_txt.mjs` で `laws_index/YYYY-MM-DD/` を生成
3. `openai_sync_vector_store.mjs` を実行してVS同期
4. 結果をコミット＆push → **完了後、自動的に `build.yml` が `--force` モードで起動**

### 3) fetch_questions.yml（問題取得）

**実行日**: 毎年6月1日（試験日・公開日に合わせて調整可）

1. PDF取得・CSV変換
2. 結果をコミット＆push → **完了後、自動的に `build.yml` が起動**

### 4) build.yml（CSV→dist生成 + Pages公開 + AI解説）

**実行タイミング**:
* `main` への push
* 上記2つのFetchワークフロー完了時 (`workflow_run`)

**解説生成ロジック**:
* 通常（6月の問題追加や手動push）: **解説が空の場合のみ**生成（スキップ機能）
* 法令更新後（9月のFetch Laws完了後）: **`--force` オプション** が付与され、全過去問の解説を最新法令で上書き再生成

**チェックポイント方式:**
5問生成するごとに自動コミットするため、途中で止まっても次回は続きから再開できます。
無限ループ防止のため、botによるコミットやdist/の変更はトリガーしません。

## トラブルシュート

### Cannot find package 'openai'
`npm i openai` して `package-lock.json` をコミットしてから Actions を回してください（`npm ci` で入るように）

### .openai/vector_store_id.txt が無い
先に `openai_sync_vector_store.mjs` を実行してVS IDを生成し、`.openai/` をコミットしておくのが最も安定です

### 429 / insufficient_quota
クレジット不足・プロジェクト上限・課金設定の可能性があります（Billing/Usage/Limits を確認）

### 400 Unsupported parameter: 'temperature'
モデルによって temperature 非対応の場合があります
→ `--temperature` を外す、または対応モデルに切り替える

### gzip/stream系エラー
`.jsonl.gz` の入出力で pipe 方向が不正だと落ちます
→ Readable -> Transform(gzip) -> Writable の順になっているか確認

### 巨大ファイル運用（laws/laws_index）
`laws/`（XML）や `laws_index/`（TXT）は大きくなります
→ 運用ポリシー（コミット対象/保持期間/スナップショット数）を決めるのがおすすめです

## ライセンス/出典について

過去問・法令データの取り扱いは、利用規約・引用要件・配布可否に注意してください
e-Gov法令XMLは公的ソースですが、アプリ配布形態や二次配布の扱いは運用方針に合わせて整理してください
