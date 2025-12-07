/**
 * AI解説付きJSONLビルドスクリプト
 * Gemini APIを使用して、法律データをRAGコンテキストとして活用し、
 * 各問題に対する詳細な解説を生成する
 */

import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'csv-parse/sync';
import { createGzip } from 'node:zlib';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { GoogleGenerativeAI } from '@google/generative-ai';
import { getLatestLawsDir, buildRagContext } from './law_parser.js';

// 設定
const DATA_DIR = './data';
const DIST_DIR = './dist_with_ai';
const BUNDLES_DIR = path.join(DIST_DIR, 'bundles');
const LAWS_DIR = './laws';

const contentVersion = process.env.CONTENT_VERSION || new Date().toISOString().slice(0, 10).replace(/-/g, '.');
const generatedAt = new Date().toISOString();

// Gemini API設定
const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
if (!GEMINI_API_KEY) {
    console.error('[build_with_ai] ERROR: GEMINI_API_KEY environment variable is required');
    process.exit(1);
}

const genAI = new GoogleGenerativeAI(GEMINI_API_KEY);
const model = genAI.getGenerativeModel({ model: 'gemini-2.0-flash-exp' });

// レート制限対策の設定
const DELAY_BETWEEN_REQUESTS_MS = 1000; // 1秒間隔
const MAX_RETRIES = 3;

/**
 * 指定ミリ秒待機する
 */
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Gemini APIで問題の解説を生成する
 * @param {object} question - 問題オブジェクト
 * @param {string} ragContext - RAGコンテキスト（法律条文）
 * @returns {string} - 生成された解説テキスト
 */
async function generateExplanation(question, ragContext) {
    const choicesText = question.choices
        .map(c => `選択肢${c.key}: ${c.text}`)
        .join('\n\n');

    const prompt = `あなたは不動産鑑定士試験の専門家です。以下の問題について、詳細な解説を生成してください。

## 問題情報
- 科目: ${question.subject}
- トピック: ${question.topic}
- 問題番号: ${question.question_no}

## 問題文
${question.statement}

## 選択肢
${choicesText}

## 正解
選択肢${question.answer}

${ragContext ? `## 関連法令
${ragContext}

` : ''}## 解説作成の指示
1. まず正解の選択肢${question.answer}がなぜ正しいのかを詳しく説明してください
2. 次に、各誤りの選択肢について、それぞれなぜ間違っているのかを具体的に説明してください
3. 関連する法律の条文がある場合は、条文番号を引用してください
4. 受験者が理解しやすいよう、ポイントを明確に説明してください

解説を日本語で作成してください。`;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        try {
            const result = await model.generateContent(prompt);
            const response = await result.response;
            return response.text();
        } catch (error) {
            console.error(`[build_with_ai] Gemini API error (attempt ${attempt}/${MAX_RETRIES}):`, error.message);

            if (attempt < MAX_RETRIES) {
                // エクスポネンシャルバックオフ
                const waitTime = Math.pow(2, attempt) * 1000;
                console.log(`[build_with_ai] Waiting ${waitTime}ms before retry...`);
                await sleep(waitTime);
            } else {
                console.error(`[build_with_ai] Failed to generate explanation for question ${question.id}`);
                return `[解説生成エラー] ${error.message}`;
            }
        }
    }

    return '';
}

/**
 * CSVファイルを正規化されたオブジェクト配列に変換する
 */
function normalizeRow(r, idx, yy, filename) {
    const id = (r.id && r.id.trim()) || `${yy}-${String(idx + 1).padStart(3, '0')}`;
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
        ai_explanation: '', // AI解説はあとで追加
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

function guessGregorian(era, eraYear) {
    if ((era || '').includes('令和') && eraYear) return 2018 + Number(eraYear);
    if ((era || '').includes('平成') && eraYear) return 1988 + Number(eraYear);
    return undefined;
}

function toTitle(era, eraYear, items) {
    const left = era && eraYear ? `${era}${eraYear}年` : '';
    return `${left} 全${items}問 (AI解説付き)`.trim();
}

function latestUpdatedAt(items) {
    const ts = items.map(i => Date.parse(i.updated_at || '')).filter(Number.isFinite);
    const max = Math.max(...ts);
    return Number.isFinite(max) ? new Date(max).toISOString() : null;
}

async function gzipWriteString(s, outPath) {
    const src = Readable.from([s]);
    const gz = createGzip();
    const ws = fs.createWriteStream(outPath);
    await pipeline(src, gz, ws);
}

// メイン処理
async function main() {
    console.log('[build_with_ai] Starting AI explanation generation...');

    // 出力ディレクトリ作成
    fs.mkdirSync(BUNDLES_DIR, { recursive: true });

    // 最新の法律データディレクトリを取得
    const lawsDir = getLatestLawsDir(LAWS_DIR);
    if (!lawsDir) {
        console.error('[build_with_ai] ERROR: No laws directory found');
        process.exit(1);
    }
    console.log(`[build_with_ai] Using laws directory: ${lawsDir}`);

    // CSVファイルを列挙
    const CSV_PATTERN = /^r\d{2}_(gyousei|kanteihyoka)\.csv$/i;
    const allEntries = fs.readdirSync(DATA_DIR).sort();
    const files = allEntries.filter(f => CSV_PATTERN.test(f)).sort();

    console.log(`[build_with_ai] Found ${files.length} CSV files:`, files);

    if (files.length === 0) {
        console.warn('[build_with_ai] No matching CSVs found');
        return;
    }

    // 年度ごとにグルーピング
    const grouped = new Map();
    for (const f of files) {
        const yy = f.slice(0, 3);
        if (!grouped.has(yy)) grouped.set(yy, []);
        grouped.get(yy).push(f);
    }

    const bundles = [];
    const t0 = Date.now();

    // 年度ごとに処理
    for (const [yy, list] of grouped.entries()) {
        console.log(`\n[build_with_ai] ==== Year ${yy} ====`);

        let items = [];
        for (const f of list) {
            const p = path.join(DATA_DIR, f);
            console.log(`[build_with_ai] Reading: ${p}`);
            const csv = fs.readFileSync(p, 'utf8');

            let rows;
            try {
                rows = parse(csv, { columns: true, skip_empty_lines: true, relax_quotes: true });
            } catch (e) {
                console.error(`[build_with_ai] CSV parse error in ${f}:`, e.message);
                throw e;
            }

            // バリデーション
            for (let i = 0; i < rows.length; i++) {
                const r = rows[i];
                const line = i + 2;
                for (let k = 1; k <= 5; k++) {
                    if (typeof r[`choice${k}`] === 'undefined') {
                        throw new Error(`${f}:${line} choice${k} 列がありません`);
                    }
                }
                const ans = Number(r.answer);
                if (!(ans >= 1 && ans <= 5)) {
                    throw new Error(`${f}:${line} answer=${r.answer} が不正（1..5）`);
                }
            }

            const normalized = rows.map((r, i) => normalizeRow(r, i, yy, f));
            items = items.concat(normalized);
        }

        // 並び替え
        items.sort((a, b) => (a.subject || '').localeCompare(b.subject || '', 'ja') || a.question_no - b.question_no);

        console.log(`[build_with_ai] Processing ${items.length} questions with AI...`);

        // 各問題にAI解説を生成
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            console.log(`[build_with_ai] Generating explanation for ${item.id} (${i + 1}/${items.length})...`);

            // RAGコンテキストを構築
            const ragContext = buildRagContext(item.topic, lawsDir);

            // AI解説を生成
            const explanation = await generateExplanation(item, ragContext);
            item.ai_explanation = explanation;

            // レート制限対策
            if (i < items.length - 1) {
                await sleep(DELAY_BETWEEN_REQUESTS_MS);
            }
        }

        // JSONL化
        const jsonl = items.map(o => JSON.stringify(o)).join('\n');

        // gzipで出力
        const outPath = path.join(BUNDLES_DIR, `${yy}.jsonl.gz`);
        console.log(`[build_with_ai] Writing: ${outPath}`);
        await gzipWriteString(jsonl, outPath);

        // ハッシュ計算
        const buf = fs.readFileSync(outPath);
        const sha256 = createHash('sha256').update(buf).digest('hex');
        console.log(`[build_with_ai] Wrote ${outPath} size=${buf.length} bytes sha256=${sha256}`);

        // manifestエントリ
        const any = items[0] || {};
        const entry = {
            id: yy,
            title: toTitle(any.era, any.era_year, items.length),
            year: Number(any.year),
            items: items.length,
            url: `/bundles/${yy}.jsonl.gz`,
            size: buf.length,
            sha256,
            etag: `W/"${yy}@${contentVersion}-ai"`,
            updated_at: latestUpdatedAt(items) || generatedAt,
            has_ai_explanation: true
        };
        bundles.push(entry);
        console.log('[build_with_ai] Manifest entry:', entry);
    }

    // manifest.json を出力
    const manifest = {
        schema_version: '1.1.0',
        content_version: contentVersion,
        generated_at: generatedAt,
        ai_model: 'gemini-2.0-flash-exp',
        bundles
    };
    fs.writeFileSync(path.join(DIST_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');

    const dt = Date.now() - t0;
    console.log(`\n[build_with_ai] Manifest written: ${path.join(DIST_DIR, 'manifest.json')}`);
    console.log(`[build_with_ai] Bundles: ${bundles.length} (ids: ${bundles.map(b => b.id).join(', ') || '-'})`);
    console.log(`[build_with_ai] Done in ${dt} ms`);
}

main().catch(err => {
    console.error('[build_with_ai] Fatal error:', err);
    process.exit(1);
});
