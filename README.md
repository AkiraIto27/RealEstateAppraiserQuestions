RealEstate Appraiser Questions / Past Questions Dataset

不動産鑑定士（短答式）過去問を 年度CSV → JSONLバンドル → manifest.json に変換し、
オフライン対応のモバイルアプリから取得しやすい形式で公開するためのリポジトリです。

原本：/data/（年度×科目のCSV）

生成物：/dist/manifest.json と /dist/bundles/rYY.jsonl.gz（年度ごとのJSONL・gzip）

配信：GitHub Pages（自動デプロイ）

5択固定（choice1〜choice5）。answerは1..5の数値

目次

ディレクトリ構成

CSVスキーマ

ビルドと公開

差分更新の仕組み

動作確認（curl）

アプリ側の利用手順

新年度データの追加手順

開発メモ / トラブルシュート

ライセンス/出典について

ディレクトリ構成
/
├─ data/                     # 原本（CSV）
│  ├─ r03_gyousei.csv
│  ├─ r03_kanteihyoka.csv
│  ├─ r04_gyousei.csv
│  └─ r04_kanteihyoka.csv
├─ dist/                     # 生成物（自動生成; コミット/Pages公開）
│  ├─ manifest.json
│  └─ bundles/
│      ├─ r03.jsonl.gz
│      └─ r04.jsonl.gz
├─ scripts/
│  └─ build.js               # CSV→JSONL & manifest 生成スクリプト
├─ .github/workflows/
│  └─ build.yml              # GitHub Actions（ビルド/コミット/Pages公開）
├─ package.json
└─ README.md

CSVスキーマ

ヘッダ（固定・21列）

id,year,era,era_year,exam,subject,topic,question_no,statement,
choice1,choice2,choice3,choice4,choice5,answer,
explanation,law_citations,difficulty,tags,source_page,updated_at


例：r05_gyousei.csv, r05_kanteihyoka.csv

5択固定。answer は 1..5 の数値

law_citations は ; 区切り（例：土地基本法:X条; 都計法:Y条）

tags はカンマ区切り（例：頻出,改正2025）

文章にカンマ/改行があるセルは引用符で囲む（Excel/スプレッドシートでOK）

ビルドと公開
ローカル
npm i
npm run build
# dist/manifest.json と dist/bundles/*.jsonl.gz が生成されます

GitHub Actions（自動）

data/ を更新して main にpush → Actionsが走り、以下を実施

scripts/build.js で dist/ を生成

dist/ を 同じブランチにコミット（履歴に残す）

dist/ を Pagesにデプロイ

必要権限：permissions: { contents: write, pages: write, id-token: write }
リポジトリ設定 → Actions → General → Workflow permissions を Read and write に

差分更新の仕組み

manifest.json には各年度バンドルの sha256 / etag / updated_at を格納

クライアントは起動時に manifest.json を取得し、年度ごとの差分だけ rYY.jsonl.gz を再DL

完全オフライン運用：一度取得した束はローカルDBに取り込み

manifest.json（例・抜粋）

{
  "schema_version": "1.1.0",
  "content_version": "2025.11.0",
  "generated_at": "2025-11-08T00:00:00Z",
  "bundles": [
    {
      "id": "r05",
      "title": "令和5年 全40問",
      "year": 2023,
      "items": 40,
      "url": "/bundles/r05.jsonl.gz",
      "size": 129112,
      "sha256": "a8b1...ff",
      "etag": "W/\"r05@2025.11.0\"",
      "updated_at": "2025-11-08T00:00:00Z"
    }
  ]
}

動作確認（curl）

<USER>/<REPO> は自分のアカウントに置き換え（例：akiraito27/RealEstateAppraiserQuestions）。

# manifest 取得
curl -sS --compressed https://<USER>.github.io/<REPO>/manifest.json | jq .

# ETag で 304 確認
ETAG=$(curl -sI https://<USER>.github.io/<REPO>/manifest.json | awk -F': ' '/^etag/i{print $2}')
curl -i -H "If-None-Match: $ETAG" https://<USER>.github.io/<REPO>/manifest.json

# バンドル取得 & 先頭レコード表示
curl -sS -o r05.jsonl.gz https://<USER>.github.io/<REPO>/bundles/r05.jsonl.gz
gunzip -c r05.jsonl.gz | head -n 1 | jq .

# SHA-256 整合性
MAN_SHA=$(curl -sS https://<USER>.github.io/<REPO>/manifest.json | jq -r '.bundles[] | select(.id=="r05") | .sha256')
LOCAL_SHA=$(sha256sum r05.jsonl.gz | awk '{print $1}')
echo "manifest:$MAN_SHA local:$LOCAL_SHA"

アプリ側の利用手順

GET /manifest.json を取得（If-None-Match を送ると帯域節約）

年度ごとに sha256/etag を比較し、変わっていれば GET /bundles/rYY.jsonl.gz

解凍 → 1行=1レコードの JSON をローカルDBへ UPSERT

判定はクライアント側で：selected === answer（answer は 1..5）

JSONL 1行（例）：

{"id":"r05-001","year":2023,"era":"令和","era_year":5,"exam":"不動産鑑定士 短答","subject":"行政法規","topic":"土地基本法","question_no":1,"statement":"…","choices":[{"key":1,"text":"(1)…"},{"key":2,"text":"(2)…"},{"key":3,"text":"(3)…"},{"key":4,"text":"(4)…"},{"key":5,"text":"(5)…"}],"answer":2,"explanation":"","law_citations":[{"law":"土地基本法","article":"X条"}],"difficulty":2,"tags":["頻出"],"source":{"paper":"令和5年 行政法規","page":3},"updated_at":"2025-11-08T00:00:00Z"}

新年度データの追加手順

/data/ に rYY_gyousei.csv と rYY_kanteihyoka.csv を追加

answer は 1..5 の数値、choice1..5 は必ず5列

PRを作成 → レビュー → main へマージ

Actions が自動で dist/ を更新 & コミット & Pages公開

開発メモ / トラブルシュート

ファイル名が拾われない
scripts/build.js は rYY_gyousei.csv / rYY_kanteihyoka.csv を対象にしています。表記ゆれがあると無視されます（ログに UNMATCHED と出力）。

CSV列ズレ
21列固定。末尾カンマ/空列で22列になると Invalid Record Length。
→ エディタで空列削除 / ヘッダを固定 / 引用符を適切に。

answerにテキストが入る
answer は 必ず数値（1..5）。誤って「ニとホ」等の本文が入るとバリデーションで落ちます。

Pagesに出るがリポジトリに無い
Pagesアーティファクト公開だけだとリポジトリには残りません。
本リポジトリは dist をコミットするワークフローを採用しています（permissions: contents: write 必須）。

CORS
GitHub Pages はGETに対して通常CORS許可（Access-Control-Allow-Origin: *）が返るため、モバイルアプリの fetch で取得可能です。