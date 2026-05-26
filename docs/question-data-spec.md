# Question Data Spec

## 鑑定評価39・40問の表形式

`不動産の鑑定評価に関する理論` の `question_no` 39・40 は、前提条件や数値表を使う表形式の計算問題として扱う。

対象:

- `subject`: `不動産の鑑定評価に関する理論`
- `question_no`: `39` または `40`
- `id`: 各年度の `rXX-079` または `rXX-080`

## exam

この表形式問題は `exam=calc_numeric` に統一する。

`r08-079` のように、表中の空欄ラベル（例: `ア`〜`エ`）に当てはまる数値の組合せを選ぶ形式でも、問われている内容は前提条件・数値に基づく計算結果であるため `fill_blank` ではなく `calc_numeric` とする。

## tags

この表形式問題には `tags=markdown_table` を付与する。

既存タグがある場合はカンマ区切りで追記する。

## statement

CSVでは1レコードを1物理行に保ち、`statement` 内の改行は実改行ではなく `\n` エスケープで保持する。

表はMarkdown風のパイプ区切りで記述する。

```text
【前提条件】\n| 項目 | 数値 |\n|---|---|\n| 更地価格 | 50,000,000 円 |\n| 期待利回り | 4.0 ％ |
```

JSONLでは1行1問を維持し、`statement` 内の改行はJSON文字列内の `\\n` として保持する。

## Rendering

アプリ側では、対象問題の `statement` を `\n` から実改行へ復元し、Markdown風テーブルブロックをネイティブの表として描画する。

判定条件は以下を基本とする。

- `tags` に `markdown_table` が含まれる
- `subject == "不動産の鑑定評価に関する理論"`
- `question_no == 39 || question_no == 40`
- `statement` にMarkdown風テーブル行が含まれる

`exam=calc_numeric` は出題形式の分類として保持し、表描画の補助情報として使ってよい。ただし、表描画の主条件は `markdown_table` タグ、上記の対象問題条件、Markdown風テーブルの存在とする。

## Validation

CSV編集後は以下を実行する。

```bash
npm run exam:check
npm run build
npm run sync:ai-copy
node scripts/update_manifest.mjs --dist dist_with_ai
npm run exam:check
```

`npm run exam:check` は、鑑定評価39・40問のMarkdown風テーブルを `calc_numeric` と推定する。
