#!/usr/bin/env node
/**
 * dist/bundles/*.jsonl.gz を読み、explanation が空の問題だけ
 * OpenAI Responses API + file_search(Vector Store) で解説を生成して上書きする。
 *
 * Usage:
 *   OPENAI_API_KEY=... node scripts/generate_explanations.mjs
 *
 * Options:
 *   --dist dist
 *   --bundles dist/bundles
 *   --model gpt-4o-mini
 *   --temperature 0.2
 *   --max-results 8
 *   --limit 50          # 最大処理件数（テスト用）
 *   --dry-run           # 生成はするがファイルを書き換えない
 */

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";
import readline from "node:readline";
import OpenAI from "openai";

const args = process.argv.slice(2);
const getArg = (name, fallback) => {
    const i = args.indexOf(name);
    if (i === -1) return fallback;
    const v = args[i + 1];
    return v ?? fallback;
};
const hasFlag = (name) => args.includes(name);

const DIST_DIR = getArg("--dist", "dist");
const BUNDLES_DIR = getArg("--bundles", path.join(DIST_DIR, "bundles"));
const MODEL = getArg("--model", "gpt-4o-mini");
const TEMPERATURE = Number(getArg("--temperature", "0.2"));
const MAX_RESULTS = Number(getArg("--max-results", "8"));
const LIMIT = Number(getArg("--limit", "0")); // 0 = unlimited
const DRY_RUN = hasFlag("--dry-run");

const VS_ID_FILE = path.join(".openai", "vector_store_id.txt");

const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

function normalizeChoices(q) {
    // choices: [{key:1,text:"..."}] / ["..."] / {1:"...",2:"..."} 等を一応吸収
    const c = q.choices ?? q.options ?? q.choices_list;
    if (!c) return null;

    if (Array.isArray(c)) {
        return c.map((x, idx) => {
            if (typeof x === "string") return { key: idx + 1, text: x };
            if (typeof x === "object" && x) return { key: x.key ?? (idx + 1), text: x.text ?? x.label ?? "" };
            return { key: idx + 1, text: String(x) };
        });
    }

    if (typeof c === "object") {
        return Object.keys(c)
            .sort((a, b) => Number(a) - Number(b))
            .map((k) => ({ key: isNaN(Number(k)) ? k : Number(k), text: String(c[k]) }));
    }

    return null;
}

function isExplanationEmpty(q) {
    const e = q.explanation;
    return e == null || (typeof e === "string" && e.trim() === "");
}

function safeJsonParse(line) {
    try {
        return JSON.parse(line);
    } catch {
        return null;
    }
}

async function listBundleFiles(dir) {
    if (!fs.existsSync(dir)) return [];
    const files = (await fsp.readdir(dir))
        .filter((f) => f.endsWith(".jsonl.gz"))
        .map((f) => path.join(dir, f))
        .sort();
    return files;
}

async function readVectorStoreId() {
    const id = (await fsp.readFile(VS_ID_FILE, "utf-8")).trim();
    if (!id) throw new Error(`Empty vector_store_id in ${VS_ID_FILE}`);
    return id;
}

// output_text が無い場合のフォールバック（SDKやモード差）
function getOutputText(resp) {
    if (typeof resp.output_text === "string" && resp.output_text.trim()) return resp.output_text;

    // 念のため output をなめて output_text を集める
    const out = [];
    for (const item of resp.output ?? []) {
        if (item?.type !== "message") continue;
        for (const c of item.content ?? []) {
            if (c?.type === "output_text" && typeof c.text === "string") out.push(c.text);
        }
    }
    return out.join("\n").trim();
}

const SCHEMA = {
    type: "object",
    properties: {
        explanation: { type: "string" },
        law_citations: { type: "array", items: { type: "string" } },
    },
    required: ["explanation", "law_citations"],
    additionalProperties: false,
};

function buildPrompt(q) {
    const choices = normalizeChoices(q);
    const answer = q.answer ?? q.correct_answer ?? q.correct ?? null;

    const meta = {
        id: q.id,
        year: q.year,
        era: q.era,
        era_year: q.era_year,
        exam: q.exam,
        subject: q.subject,
        topic: q.topic,
        question_no: q.question_no,
        source: q.source,
    };

    return {
        meta,
        statement: q.statement ?? q.question ?? q.stem ?? "",
        choices,
        answer,
    };
}

function instructionsJa() {
    return [
        "あなたは不動産鑑定士試験（短答）の解説作成者です。",
        "必ず file_search で取得できた法令の記載に基づいて解説してください。取得できない内容は推測しないでください。",
        "根拠条文を特定できない場合は、その旨を explanation に明記し、law_citations は空配列にしてください。",
        "出力は日本語で、構成は次の順にしてください：",
        "1) 正解はX番。 2) 理由（法令根拠ベース） 3) 各選択肢が正誤になる理由（1〜5または提示されたchoicesに対応）",
        "law_citations には、参照した条文を '法令名 第○条（必要なら項・号）' のような文字列で列挙してください（複数可）。",
    ].join("\n");
}

async function callOpenAI(vsId, qObj) {
    // ここで “問題1問ぶんだけ” を投げる。巨大JSONをまとめて投げない。
    const resp = await client.responses.create({
        model: MODEL,
        temperature: TEMPERATURE,
        store: false, // ログ保存不要なら false（任意）
        instructions: instructionsJa(),
        tools: [
            {
                type: "file_search",
                vector_store_ids: [vsId],
                max_num_results: MAX_RESULTS,
            },
        ],
        input: [
            {
                role: "user",
                content: JSON.stringify(qObj, null, 0),
            },
        ],
        text: {
            format: {
                type: "json_schema",
                name: "explanation_result",
                schema: SCHEMA,
                strict: true,
            },
        },
    });

    const txt = getOutputText(resp);
    if (!txt) throw new Error("Empty model output");

    let parsed;
    try {
        parsed = JSON.parse(txt);
    } catch {
        throw new Error(`Model output is not JSON: ${txt.slice(0, 200)}...`);
    }

    // 最低限の検証
    if (typeof parsed.explanation !== "string") throw new Error("Invalid: explanation");
    if (!Array.isArray(parsed.law_citations)) throw new Error("Invalid: law_citations");

    return parsed;
}

// 簡単なリトライ（429/5xx）
async function withRetry(fn, { tries = 4 } = {}) {
    let lastErr;
    for (let i = 0; i < tries; i++) {
        try {
            return await fn();
        } catch (e) {
            lastErr = e;
            const msg = String(e?.message ?? e);
            const isRetryable =
                msg.includes("429") ||
                msg.includes("rate limit") ||
                msg.includes("503") ||
                msg.includes("502") ||
                msg.includes("timeout") ||
                msg.includes("ETIMEDOUT");
            if (!isRetryable || i === tries - 1) throw e;
            const wait = 1500 * Math.pow(2, i);
            await new Promise((r) => setTimeout(r, wait));
        }
    }
    throw lastErr;
}

async function processOneBundle(vsId, bundlePath) {
    const tmpOut = bundlePath + ".tmp";
    const inStream = fs.createReadStream(bundlePath).pipe(zlib.createGunzip());
    const rl = readline.createInterface({ input: inStream, crlfDelay: Infinity });

    const outStream = fs.createWriteStream(tmpOut).pipe(zlib.createGzip({ level: 9 }));

    let processed = 0;
    let generated = 0;
    let skipped = 0;

    for await (const line of rl) {
        if (LIMIT > 0 && generated >= LIMIT) {
            // LIMIT 到達後も “残りはそのまま” 書き出して整合性を保つ
            outStream.write(line + "\n");
            continue;
        }

        const obj = safeJsonParse(line);
        if (!obj) {
            // 壊れ行はそのまま
            outStream.write(line + "\n");
            continue;
        }

        processed++;

        if (!isExplanationEmpty(obj)) {
            skipped++;
            outStream.write(JSON.stringify(obj) + "\n");
            continue;
        }

        const promptObj = buildPrompt(obj);

        const result = await withRetry(() => callOpenAI(vsId, promptObj));
        obj.explanation = result.explanation;
        obj.law_citations = result.law_citations;

        generated++;

        // 進捗ログ
        if (generated % 5 === 0) {
            console.log(`[${path.basename(bundlePath)}] generated=${generated} skipped=${skipped} processed=${processed}`);
        }

        outStream.write(JSON.stringify(obj) + "\n");
    }

    await new Promise((r) => outStream.end(r));

    if (DRY_RUN) {
        await fsp.unlink(tmpOut);
        console.log(`[dry-run] ${bundlePath}: would update (generated=${generated}, skipped=${skipped})`);
        return { processed, generated, skipped, updated: false };
    }

    // 元ファイル置換
    await fsp.rename(tmpOut, bundlePath);
    console.log(`${bundlePath}: updated (generated=${generated}, skipped=${skipped}, processed=${processed})`);
    return { processed, generated, skipped, updated: true };
}

async function maybeUpdateManifest(distDir) {
    // manifestがハッシュ等を持っていた場合のため “あれば更新” できるようにする（無ければ何もしない）
    const manifestPath = path.join(distDir, "manifest.json");
    if (!fs.existsSync(manifestPath)) return;

    try {
        const raw = await fsp.readFile(manifestPath, "utf-8");
        const m = JSON.parse(raw);

        // 一般的なフィールドを安全に更新（存在しなければ触らない）
        if (typeof m === "object" && m) {
            if ("updated_at" in m) m.updated_at = new Date().toISOString();
            if ("generated_at" in m) m.generated_at = new Date().toISOString();
        }

        await fsp.writeFile(manifestPath, JSON.stringify(m, null, 2) + "\n", "utf-8");
    } catch {
        // manifest構造が違う/JSONでない等は無視
    }
}

async function main() {
    if (!process.env.OPENAI_API_KEY) {
        throw new Error("OPENAI_API_KEY is required.");
    }
    const vsId = await readVectorStoreId();

    const bundleFiles = await listBundleFiles(BUNDLES_DIR);
    if (bundleFiles.length === 0) {
        throw new Error(`No bundle files found: ${BUNDLES_DIR}/*.jsonl.gz`);
    }

    console.log(`Vector store: ${vsId}`);
    console.log(`Model       : ${MODEL}`);
    console.log(`Bundles     : ${bundleFiles.length}`);
    console.log(`Dry-run     : ${DRY_RUN}`);

    let totalGen = 0;
    for (const b of bundleFiles) {
        const r = await processOneBundle(vsId, b);
        totalGen += r.generated;
    }

    if (!DRY_RUN) {
        await maybeUpdateManifest(DIST_DIR);
    }

    console.log(`Done. total_generated=${totalGen}`);
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
