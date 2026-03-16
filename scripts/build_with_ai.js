/**
 * AI解説付きJSONLビルドスクリプト（バッチ処理版）
 * 
 * 既存のdist/bundles/*.jsonl.gzを読み込み、法律データをRAGとして
 * 年度ごとに1回のAPIリクエストで全問題の解説を一括生成する
 */

// .envファイルから環境変数を読み込む（ローカル開発用）
try {
    await import('dotenv/config');
} catch {
    // dotenvがない場合は無視（GitHub Actionsでは環境変数が直接設定される）
}

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

const contentVersion = process.env.CONTENT_VERSION || new Date().toISOString().slice(0, 16).replace('T', '.').replace(/-/g, '.').replace(':', '');
const generatedAt = new Date().toISOString();

// Gemini API設定
const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
if (!GEMINI_API_KEY) {
    console.error('[build_with_ai] ERROR: GEMINI_API_KEY environment variable is required');
    process.exit(1);
}

const genAI = new GoogleGenerativeAI(GEMINI_API_KEY);
const model = genAI.getGenerativeModel({ model: 'gemini-2.5-flash' });

// レート制限対策の設定
const DELAY_BETWEEN_REQUESTS_MS = 60000; // 1分間隔
const MAX_RETRIES = 5;

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
                const articleTexts = articles.map(a =>
                    `${a.title}${a.caption ? `（${a.caption}）` : ''}: ${a.content}`
                ).join('\n');
                lawTexts.push(`【${lawName}】\n${articleTexts}`);
            }
        } catch (e) {
            // パースエラーは無視
        }
    }

    // 最大文字数を制限（Geminiのコンテキスト制限対策）
    const combined = lawTexts.join('\n\n');
    // 最大文字数を制限（Gemini 1.5 Proのコンテキストは2Mトークン以上あるため、十分に大きく取る）
    return combined.length > 3000000 ? combined.substring(0, 3000000) + '\n...(省略)' : combined;
}

/**
 * 年度の全問題に対してAI解説を一括生成する
 */
async function generateExplanationsForChunk(questions, lawsContext, yearId, topicName) {
    // console.log(`[build_with_ai] Generating for ${questions.length} questions (Topic: ${topicName})`);

    const questionsText = questions.map((q) => {
        const choicesText = q.choices.map(c => `${c.key}: ${c.text.substring(0, 100)}`).join(' | ');
        return `問${q.question_no}: ${q.statement.substring(0, 150)}... 選択肢: ${choicesText} 正解: ${q.answer}`;
    }).join('\n\n');

    const prompt = `あなたは不動産鑑定士試験の専門家です。以下の${questions.length}問の問題（トピック: ${topicName}）について、それぞれ解説を生成してください。

## 関連法令（参考資料）
${lawsContext ? lawsContext.substring(0, 30000) : '(関連法令なし)'}

## 問題一覧
${questionsText}

## 出力形式
以下のJSON配列形式で出力してください。問題番号順に${questions.length}個の解説を含めてください：

\`\`\`json
[
  {
    "question_no": ${questions[0].question_no},
    "ai_explanation": "【正解】選択肢Xが正解です。\\n\\n【正解の理由】...\\n\\n【各選択肢の解説】\\n選択肢1: ...\\n選択肢2: ...\\n..."
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
            // console.log(`[build_with_ai] Sending request for topic ${topicName} (attempt ${attempt})`);
            const result = await model.generateContent(prompt);
            const response = await result.response;
            const text = response.text();

            const jsonMatch = text.match(/\[[\s\S]*\]/);
            if (jsonMatch) {
                const explanations = JSON.parse(jsonMatch[0]);
                console.log(`[build_with_ai] Received ${explanations.length} explanations for topic: ${topicName}`);
                return explanations;
            } else {
                throw new Error('JSON array not found');
            }
        } catch (error) {
            console.error(`[build_with_ai] Error topic ${topicName} (attempt ${attempt}):`, error.message);
            if (attempt < MAX_RETRIES) {
                await sleep(attempt * 10000);
            }
        }
    }
    console.warn(`[build_with_ai] Failed to generate for topic: ${topicName}`);
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

        // トピックごとにグループ化
        const questionsByTopic = {};
        for (const q of questions) {
            const topic = q.topic || 'その他';
            if (!questionsByTopic[topic]) questionsByTopic[topic] = [];
            questionsByTopic[topic].push(q);
        }

        const topics = Object.keys(questionsByTopic);
        console.log(`[build_with_ai] Found ${topics.length} topics:`, topics);

        // トピックごとに解説生成
        let allExplanations = [];
        for (let t = 0; t < topics.length; t++) {
            const topic = topics[t];
            const topicQuestions = questionsByTopic[topic];
            console.log(`[build_with_ai] Processing topic: ${topic} (${topicQuestions.length} questions)`);

            // そのトピックに関連する法律コンテキストのみ構築
            const context = buildRagContext(topic, lawsDir);

            // 解説生成
            const explanations = await generateExplanationsForChunk(topicQuestions, context, yearId, topic);
            allExplanations = allExplanations.concat(explanations);

            // レート制限対策の待機
            await sleep(5000);
        }

        // 解説をマージ
        let matchCount = 0;
        for (const question of questions) {
            const exp = allExplanations.find(e => e.question_no === question.question_no);
            if (exp) {
                question.ai_explanation = exp.ai_explanation;
                matchCount++;
            } else {
                console.warn(`[build_with_ai] Warning: No explanation generated for Q${question.question_no}`);
            }
        }
        console.log(`[build_with_ai] Merged ${matchCount}/${questions.length} explanations`);

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

        // 次のファイルまで待機
        if (i < filesToProcess.length - 1) {
            console.log(`[build_with_ai] Waiting ${DELAY_BETWEEN_REQUESTS_MS / 1000}s before next file...`);
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
