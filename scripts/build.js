// scripts/build.js
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'csv-parse/sync';
import { createGzip } from 'node:zlib';
import { createHash } from 'node:crypto';

const DATA_DIR = './data';
const DIST_DIR = './dist';
const BUNDLES_DIR = path.join(DIST_DIR, 'bundles');

const contentVersion = process.env.CONTENT_VERSION || new Date().toISOString().slice(0, 10).replace(/-/g, '.');
const generatedAt = new Date().toISOString();

fs.mkdirSync(BUNDLES_DIR, { recursive: true });

// 1) data配下のCSVを列挙（rYY_*.csv）
const files = fs.readdirSync(DATA_DIR)
  .filter(f => /^r\d{2}_(gyousei|kanteihyoka)\.csv$/.test(f))
  .sort();

// 2) 年度ごとにグルーピング
const grouped = new Map(); // key: rYY, val: string[] files
for (const f of files) {
  const yy = f.slice(0, 3); // 'r07'
  if (!grouped.has(yy)) grouped.set(yy, []);
  grouped.get(yy).push(f);
}

const bundles = [];

for (const [yy, list] of grouped.entries()) {
  let items = [];
  for (const f of list) {
    const csv = fs.readFileSync(path.join(DATA_DIR, f), 'utf8');
    const rows = parse(csv, { columns: true, skip_empty_lines: true, relax_quotes: true });
    items = items.concat(rows.map((r, i) => normalizeRow(r, i, yy, f)));
  }

  // question_noなどの安定順に並べる（任意）
  items.sort((a, b) => (a.subject || '').localeCompare(b.subject || '', 'ja') || a.question_no - b.question_no);

  const jsonl = items.map(o => JSON.stringify(o)).join('\n');

  // gzipで出力
  const outPath = path.join(BUNDLES_DIR, `${yy}.jsonl.gz`);
  gzipWriteString(jsonl, outPath);

  // ハッシュ計算
  const buf = fs.readFileSync(outPath);
  const sha256 = createHash('sha256').update(buf).digest('hex');

  // manifestエントリ
  const any = items[0] || {};
  bundles.push({
    id: yy,                                    // r07
    title: toTitle(any.era, any.era_year, items.length),
    year: Number(any.year),
    items: items.length,
    url: `/bundles/${yy}.jsonl.gz`,
    size: buf.length,
    sha256,
    etag: `W/"${yy}@${contentVersion}"`,
    updated_at: latestUpdatedAt(items) || generatedAt
  });
}

// manifest.json
const manifest = {
  schema_version: '1.1.0',
  content_version: contentVersion,
  generated_at: generatedAt,
  bundles
};
fs.writeFileSync(path.join(DIST_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');

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

function gzipWriteString(s, outPath) {
  const gz = createGzip();
  const ws = fs.createWriteStream(outPath);
  const src = new (require('stream').Readable)({ read() { } });
  return new Promise((resolve, reject) => {
    src.push(s); src.push(null);
    src.pipe(gz).pipe(ws).on('finish', resolve).on('error', reject);
  });
}
