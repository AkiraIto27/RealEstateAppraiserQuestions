import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'csv-parse/sync';
import { createGzip } from 'node:zlib';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const DATA_DIR = './data';
const DIST_DIR = './dist';
const BUNDLES_DIR = path.join(DIST_DIR, 'bundles');

const contentVersion = process.env.CONTENT_VERSION || new Date().toISOString().slice(0, 10).replace(/-/g, '.');
const generatedAt = new Date().toISOString();

// 出力ディレクトリ作成
fs.mkdirSync(BUNDLES_DIR, { recursive: true });

// ----- 1) data配下のCSVを列挙（まずは全部）
const allEntries = fs.readdirSync(DATA_DIR).sort();
const allCsvs = allEntries.filter(f => /\.csv$/i.test(f));
console.log(`[build] data entries: ${allEntries.length}`, allEntries);
console.log(`[build] csv files found: ${allCsvs.length}`, allCsvs);

// 想定パターンにマッチ（rYY_gyousei|kanteihyoka）
const CSV_PATTERN = /^r\d{2}_(gyousei|kanteihyoka)\.csv$/i;
const files = allCsvs.filter(f => CSV_PATTERN.test(f)).sort();
const unmatched = allCsvs.filter(f => !CSV_PATTERN.test(f));
console.log(`[build] matched csv: ${files.length}`, files);
if (unmatched.length) console.warn(`[build] UNMATCHED csv (ignored): ${unmatched.length}`, unmatched);

if (files.length === 0) {
  console.warn('[build] No matching CSVs (expected like r03_gyousei.csv / r03_kanteihyoka.csv). Build continues to write empty manifest.');
}

// ----- 2) 年度ごとにグルーピング
const grouped = new Map(); // key: rYY, val: string[] files
for (const f of files) {
  const yy = f.slice(0, 3); // 'r07'
  if (!grouped.has(yy)) grouped.set(yy, []);
  grouped.get(yy).push(f);
}
console.log(`[build] years detected: ${grouped.size}`, Array.from(grouped.keys()));

const bundles = [];
const t0 = Date.now();

// ----- 3) 年度ごとに処理
for (const [yy, list] of grouped.entries()) {
  console.log(`\n[build] ==== Year ${yy} ====`);
  console.log(`[build] files for ${yy}:`, list);

  let items = [];
  for (const f of list) {
    const p = path.join(DATA_DIR, f);
    console.log(`[build] read: ${p}`);
    const csv = fs.readFileSync(p, 'utf8');
    let rows;
    try {
      rows = parse(csv, { columns: true, skip_empty_lines: true, relax_quotes: true });
    } catch (e) {
      console.error(`[build] CSV parse error in ${f}:`, e.message);
      // 解析失敗時は詳細を出力して終了
      throw e;
    }
    console.log(`[build] parsed rows from ${f}: ${rows.length}`);

    // 軽いバリデーション（任意）
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const line = i + 2; // ヘッダ1行目
      for (let k = 1; k <= 5; k++) {
        if (typeof r[`choice${k}`] === 'undefined') {
          throw new Error(`${f}:${line} choice${k} 列がありません（ヘッダ不足/列ズレの可能性）`);
        }
      }
      const ans = Number(r.answer);
      if (!(ans >= 1 && ans <= 5)) {
        throw new Error(`${f}:${line} answer=${r.answer} が不正（1..5）`);
      }
    }

    const normalized = rows.map((r, i) => normalizeRow(r, i, yy, f));
    items = items.concat(normalized);
    console.log(`[build] normalized items added from ${f}: ${normalized.length} (total: ${items.length})`);
  }

  // 並び替え（任意）
  items.sort((a, b) => (a.subject || '').localeCompare(b.subject || '', 'ja') || a.question_no - b.question_no);
  console.log(`[build] sorted items for ${yy}: ${items.length}`);

  // JSONL化
  const jsonl = items.map(o => JSON.stringify(o)).join('\n');

  // gzipで出力
  const outPath = path.join(BUNDLES_DIR, `${yy}.jsonl.gz`);
  console.log(`[build] write gzip: ${outPath}`);
  await gzipWriteString(jsonl, outPath);

  // ハッシュ計算
  const buf = fs.readFileSync(outPath);
  const sha256 = createHash('sha256').update(buf).digest('hex');
  console.log(`[build] wrote ${outPath} size=${buf.length} bytes sha256=${sha256}`);

  // manifestエントリ
  const any = items[0] || {};
  const entry = {
    id: yy,                                    // r07
    title: toTitle(any.era, any.era_year, items.length),
    year: Number(any.year),
    items: items.length,
    url: `/bundles/${yy}.jsonl.gz`,
    size: buf.length,
    sha256,
    etag: `W/"${yy}@${contentVersion}"`,
    updated_at: latestUpdatedAt(items) || generatedAt
  };
  bundles.push(entry);
  console.log('[build] manifest entry:', entry);
}

// ----- 4) manifest.json を出力
const manifest = {
  schema_version: '1.1.0',
  content_version: contentVersion,
  generated_at: generatedAt,
  bundles
};
fs.writeFileSync(path.join(DIST_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');

const dt = Date.now() - t0;
console.log(`\n[build] manifest written: ${path.join(DIST_DIR, 'manifest.json')}`);
console.log(`[build] bundles: ${bundles.length} (ids: ${bundles.map(b => b.id).join(', ') || '-'})`);
console.log(`[build] done in ${dt} ms`);

// ---------- helpers ----------
function normalizeRow(r, idx, yy, filename) {
  // idが空なら採番: rYY-xxx（CSV内の並び順）
  const id = (r.id && r.id.trim()) || `${yy}-${String(idx + 1).padStart(3, '0')}`;

  // 年の正規化（西暦>和暦推測）
  const year = Number(r.year || guessGregorian(r.era, r.era_year));

  const choices = [1, 2, 3, 4, 5].map(k => {
    const txt = (r[`choice${k}`] ?? '').toString().trim();
    return txt ? { key: k, text: txt } : null;
  }).filter(Boolean);

  const law_citations = (r.law_citations || '')
    .split(';').map(s => s.trim()).filter(Boolean)
    .map(s => {
      const [law, article = ''] = s.split(':').map(x => x.trim());
      return { law, article };
    });

  const tags = (r.tags || '')
    .split(',').map(s => s.trim()).filter(Boolean);

  // subjectはCSV名からも補完（誤入力対策）
  const subjectHint = filename.includes('gyousei') ? '行政法規'
    : filename.includes('kanteihyoka') ? '鑑定評価法規'
      : (r.subject || '');

  return {
    id,
    year,
    era: r.era || '',
    era_year: r.era_year ? Number(r.era_year) : undefined,
    exam: r.exam || '不動産鑑定士 短答',
    subject: r.subject?.trim() || subjectHint,
    topic: r.topic || '',
    question_no: Number(r.question_no || 0),
    statement: (r.statement || '').toString(),
    choices,
    answer: Number(r.answer),
    explanation: r.explanation || '',
    law_citations,
    difficulty: r.difficulty ? Number(r.difficulty) : undefined,
    tags,
    source: {
      paper: `${r.era || ''}${r.era_year || ''}年 ${r.subject || subjectHint}`.trim(),
      page: r.source_page ? Number(r.source_page) : undefined
    },
    updated_at: r.updated_at?.trim() || new Date().toISOString()
  };
}

function toTitle(era, eraYear, items) {
  const left = era && eraYear ? `${era}${eraYear}年` : '';
  return `${left} 全${items}問`.trim();
}

function latestUpdatedAt(items) {
  const ts = items.map(i => Date.parse(i.updated_at || '')).filter(Number.isFinite);
  const max = Math.max(...ts);
  return Number.isFinite(max) ? new Date(max).toISOString() : null;
}

function guessGregorian(era, eraYear) {
  if ((era || '').includes('令和') && eraYear) return 2018 + Number(eraYear);
  // 平成等が必要なら追記
  return undefined;
}

async function gzipWriteString(s, outPath) {
  const src = Readable.from([s]);
  const gz = createGzip();
  const ws = fs.createWriteStream(outPath);
  await pipeline(src, gz, ws);
}