/**
 * AI解説付きJSONLビルドスクリプト（バッチ処理版）
 * 
 * 既存のdist/bundles/*.jsonl.gzを読み込み、法律データをRAGとして
 * 年度ごとに1回のAPIリクエストで全問題の解説を一括生成する
 */

// .envファイルから環境変数を読み込む
import 'dotenv/config';

import fs from 'node:fs';
import path from 'node:path';
import { createGzip, gunzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { GoogleGenerativeAI } from '@google/generative-ai';
import { getLatestLawsDir, buildRagContext, parseLawXml } from './law_parser.js';

// 設定
const DIST_DIR = './dist';
const BUNDLES_DIR = path.join(DIST_DIR, 'bundles');
const OUTPUT_DIR = './dist_with_ai';
const OUTPUT_BUNDLES_DIR = path.join(OUTPUT_DIR, 'bundles');
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
const model = genAI.getGenerativeModel({ model: 'gemini-3-pro-preview' });

// レート制限対策の設定
const DELAY_BETWEEN_REQUESTS_MS = 60000; // 1分間隔
const MAX_RETRIES = 3;

// バッチ分割設定（2日に分けて実行する場合）
// 例: 1日目: BATCH_START=0 BATCH_END=3 (r03, r04, r05)
//     2日目: BATCH_START=3 BATCH_END=5 (r06, r07)
const BATCH_START = process.env.BATCH_START ? Number(process.env.BATCH_START) : 0;
const BATCH_END = process.env.BATCH_END ? Number(process.env.BATCH_END) : undefined;

/**
 * 指定ミリ秒待機する
 */
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * gzipファイルを読み込んでJSONL形式のオブジェクト配列を返す
 */
function readJsonlGz(filepath) {
    const gzBuffer = fs.readFileSync(filepath);
    const jsonlContent = gunzipSync(gzBuffer).toString('utf8');
    const lines = jsonlContent.split('\n').filter(line => line.trim());
    return lines.map(line => JSON.parse(line));
}

/**
 * 全法律データを読み込んでテキストとして結合する
 */
function loadAllLawsAsText(lawsDir) {
    const files = fs.readdirSync(lawsDir).filter(f => f.endsWith('.xml'));
    const lawTexts = [];

    for (const file of files) {
        try {
            const { lawName, articles } = parseLawXml(path.join(lawsDir, file));
            if (lawName && articles.length > 0) {
                const articleTexts = articles.slice(0, 50).map(a =>
                    `${a.title}${a.caption ? `（${a.caption}）` : ''}: ${a.content.substring(0, 500)}`
                ).join('\n');
                lawTexts.push(`【${lawName}】\n${articleTexts}`);
            }
        } catch (e) {
            // パースエラーは無視
        }
    }

    // 最大文字数を制限（Geminiのコンテキスト制限対策）
    const combined = lawTexts.join('\n\n');
    return combined.length > 100000 ? combined.substring(0, 100000) + '\n...(省略)' : combined;
}

/**
 * 年度の全問題に対してAI解説を一括生成する
 */
async function generateExplanationsForYear(questions, lawsContext, yearId) {
    // 問題リストを作成（簡潔に）
    const questionsText = questions.map((q, i) => {
        const choicesText = q.choices.map(c => `${c.key}: ${c.text.substring(0, 100)}`).join(' | ');
        return `問${q.question_no}[${q.topic}]: ${q.statement.substring(0, 150)}... 選択肢: ${choicesText} 正解: ${q.answer}`;
    }).join('\n\n');

    const prompt = `あなたは不動産鑑定士試験の専門家です。以下の${questions.length}問の問題について、それぞれ解説を生成してください。

## 関連法令（参考資料）
${lawsContext.substring(0, 50000)}

## 問題一覧
${questionsText}

## 出力形式
以下のJSON配列形式で出力してください。問題番号順に${questions.length}個の解説を含めてください：

\`\`\`json
[
  {
    "question_no": 1,
    "ai_explanation": "【正解】選択肢Xが正解です。\\n\\n【正解の理由】...\\n\\n【各選択肢の解説】\\n選択肢1: ...\\n選択肢2: ...\\n選択肢3: ...\\n選択肢4: ...\\n選択肢5: ..."
  },
  ...
]
\`\`\`

各解説では：
1. 正解の選択肢がなぜ正しいか詳しく説明
2. 各誤りの選択肢がなぜ間違っているか具体的に説明
3. 関連する法律条文がある場合は引用

JSON配列のみを出力してください。`;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        try {
            console.log(`[build_with_ai] Sending request for ${yearId} (attempt ${attempt}/${MAX_RETRIES})...`);
            const result = await model.generateContent(prompt);
            const response = await result.response;
            const text = response.text();

            // JSONを抽出
            const jsonMatch = text.match(/\[[\s\S]*\]/);
            if (jsonMatch) {
                const explanations = JSON.parse(jsonMatch[0]);
                console.log(`[build_with_ai] Received ${explanations.length} explanations for ${yearId}`);
                return explanations;
            } else {
                throw new Error('JSON array not found in response');
            }
        } catch (error) {
            console.error(`[build_with_ai] Error (attempt ${attempt}/${MAX_RETRIES}):`, error.message);

            if (attempt < MAX_RETRIES) {
                const waitTime = Math.pow(2, attempt) * 1000;
                console.log(`[build_with_ai] Waiting ${waitTime}ms before retry...`);
                await sleep(waitTime);
            } else {
                console.error(`[build_with_ai] STOPPING: Failed after ${MAX_RETRIES} retries for ${yearId}. Exiting.`);
                process.exit(1);
            }
        }
    }

    return [];
}

async function gzipWriteString(s, outPath) {
    const src = Readable.from([s]);
    const gz = createGzip();
    const ws = fs.createWriteStream(outPath);
    await pipeline(src, gz, ws);
}

// メイン処理
async function main() {
    console.log('[build_with_ai] Starting batch AI explanation generation...');

    // 出力ディレクトリ作成
    fs.mkdirSync(OUTPUT_BUNDLES_DIR, { recursive: true });

    // 最新の法律データを読み込む
    const lawsDir = getLatestLawsDir(LAWS_DIR);
    if (!lawsDir) {
        console.error('[build_with_ai] ERROR: No laws directory found');
        process.exit(1);
    }
    console.log(`[build_with_ai] Loading laws from: ${lawsDir}`);
    const lawsContext = loadAllLawsAsText(lawsDir);
    console.log(`[build_with_ai] Laws context: ${lawsContext.length} chars`);

    // 既存のJSONLファイルを列挙
    const jsonlFiles = fs.readdirSync(BUNDLES_DIR)
        .filter(f => f.endsWith('.jsonl.gz'))
        .sort();

    console.log(`[build_with_ai] Found ${jsonlFiles.length} JSONL files:`, jsonlFiles);

    if (jsonlFiles.length === 0) {
        console.warn('[build_with_ai] No JSONL files found in dist/bundles/');
        return;
    }

    const bundles = [];
    const t0 = Date.now();

    // バッチ分割を適用
    const filesToProcess = jsonlFiles.slice(BATCH_START, BATCH_END);
    console.log(`[build_with_ai] Batch range: ${BATCH_START} to ${BATCH_END ?? jsonlFiles.length}`);
    console.log(`[build_with_ai] Processing ${filesToProcess.length} files:`, filesToProcess);

    // 各JSONLファイルを処理
    for (let i = 0; i < filesToProcess.length; i++) {
        const file = filesToProcess[i];
        const yearId = file.replace('.jsonl.gz', '');

        console.log(`\n[build_with_ai] ==== Processing ${file} (${i + 1}/${filesToProcess.length}) ====`);

        // 既存のJSONLを読み込む
        const inputPath = path.join(BUNDLES_DIR, file);
        const questions = readJsonlGz(inputPath);
        console.log(`[build_with_ai] Loaded ${questions.length} questions`);

        // AI解説を一括生成
        const explanations = await generateExplanationsForYear(questions, lawsContext, yearId);

        // 解説をマージ
        for (const question of questions) {
            const exp = explanations.find(e => e.question_no === question.question_no);
            question.ai_explanation = exp ? exp.ai_explanation : '';
        }

        // JSONL化して出力
        const jsonl = questions.map(o => JSON.stringify(o)).join('\n');
        const outPath = path.join(OUTPUT_BUNDLES_DIR, file);
        console.log(`[build_with_ai] Writing: ${outPath}`);
        await gzipWriteString(jsonl, outPath);

        // ハッシュ計算
        const buf = fs.readFileSync(outPath);
        const sha256 = createHash('sha256').update(buf).digest('hex');
        console.log(`[build_with_ai] Wrote ${outPath} size=${buf.length} bytes`);

        // manifestエントリ
        const any = questions[0] || {};
        bundles.push({
            id: yearId,
            title: `${any.era || ''}${any.era_year || ''}年 全${questions.length}問 (AI解説付き)`,
            year: Number(any.year) || 0,
            items: questions.length,
            url: `/bundles/${file}`,
            size: buf.length,
            sha256,
            etag: `W/"${yearId}@${contentVersion}-ai"`,
            updated_at: generatedAt,
            has_ai_explanation: true
        });

        // 次のリクエストまで待機（最後以外）
        if (i < jsonlFiles.length - 1) {
            console.log(`[build_with_ai] Waiting ${DELAY_BETWEEN_REQUESTS_MS / 1000}s before next request...`);
            await sleep(DELAY_BETWEEN_REQUESTS_MS);
        }
    }

    // manifest.json を出力（既存のものとマージ）
    const manifestPath = path.join(OUTPUT_DIR, 'manifest.json');
    let existingBundles = [];

    // 既存のmanifestがあれば読み込んでマージ
    if (fs.existsSync(manifestPath)) {
        try {
            const existingManifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
            existingBundles = existingManifest.bundles || [];
            console.log(`[build_with_ai] Found existing manifest with ${existingBundles.length} bundles`);
        } catch (e) {
            console.warn('[build_with_ai] Could not read existing manifest, creating new one');
        }
    }

    // 新しいbundlesと既存のbundlesをマージ（同じIDは新しいもので上書き）
    const bundleMap = new Map();
    for (const b of existingBundles) {
        bundleMap.set(b.id, b);
    }
    for (const b of bundles) {
        bundleMap.set(b.id, b);
    }
    const mergedBundles = Array.from(bundleMap.values()).sort((a, b) => a.id.localeCompare(b.id));

    const manifest = {
        schema_version: '1.1.0',
        content_version: contentVersion,
        generated_at: generatedAt,
        ai_model: 'gemini-3-pro-preview',
        bundles: mergedBundles
    };
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8');

    const dt = Date.now() - t0;
    console.log(`\n[build_with_ai] Manifest written: ${path.join(OUTPUT_DIR, 'manifest.json')}`);
    console.log(`[build_with_ai] Processed ${bundles.length} bundles`);
    console.log(`[build_with_ai] Done in ${Math.round(dt / 1000)}s`);
}

main().catch(err => {
    console.error('[build_with_ai] Fatal error:', err);
    process.exit(1);
});
