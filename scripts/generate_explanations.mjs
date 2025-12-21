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
 *   --bundle r03.jsonl.gz # 特定の1ファイルだけ処理（任意）
 *   --model gpt-5-mini
 *   --temperature 0.2
 *   --max-results 8
 *   --limit 50          # 最大生成件数（1ファイルあたり）
 *   --log-per-question  # 1問ごとの時間・トークンを出す（調査用）
 *   --dry-run           # 生成はするがファイルを書き換えない
 *   --force             # 既存の解説があっても強制的にスキップせず再生成する
 */

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";
import readline from "node:readline";
import { once } from "node:events";
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
const MODEL = getArg("--model", "gpt-5-mini");
const TEMPERATURE = Number(getArg("--temperature", "0.2"));
const MAX_RESULTS = Number(getArg("--max-results", "5"));
const LIMIT = Number(getArg("--limit", "0")); // 0 = unlimited
const DRY_RUN = hasFlag("--dry-run");
const FORCE = hasFlag("--force");
const ONLY_BUNDLE = getArg("--bundle", "");
const DEBUG_FIRST_N = parseInt(process.env.DEBUG_FIRST_N ?? "0", 10);
const LOG_PER_QUESTION = hasFlag("--log-per-question");

const VS_ID_FILE = path.join(".openai", "vector_store_id.txt");

const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const OPENAI_STATS = {
    attempts: 0, // responses.create を試した回数（リトライ込み）
    success: 0, // responses.create が成功して返った回数
    retries: 0, // リトライした回数
    total_ms: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
};

function normalizeChoices(q) {
    // choices: [{key:1,text:"..."}] / ["..."] / {1:"...",2:"..."} 等を一応吸収
    const c = q.choices ?? q.options ?? q.choices_list;
    if (!c) return null;

    if (Array.isArray(c)) {
        return c.map((x, idx) => {
            if (typeof x === "string") return { key: idx + 1, text: x };
            if (typeof x === "object" && x) return { key: x.key ?? idx + 1, text: x.text ?? x.label ?? "" };
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

async function countEmptyExplanationsInBundle(bundlePath) {
    const inStream = fs.createReadStream(bundlePath).pipe(zlib.createGunzip());
    const rl = readline.createInterface({ input: inStream, crlfDelay: Infinity });

    let total = 0;
    let empty = 0;
    let parseError = 0;

    try {
        for await (const line of rl) {
            const s = line.trim();
            if (!s) continue;

            total++;
            try {
                const obj = JSON.parse(s);
                if (isExplanationEmpty(obj)) empty++;
            } catch (e) {
                parseError++;
            }
        }
    } finally {
        rl.close();
        inStream.destroy();
    }

    return { total, empty, parseError };
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
    if (!fs.existsSync(VS_ID_FILE)) {
        throw new Error(`Vector Store ID file not found: ${VS_ID_FILE}`);
    }
    return (await fsp.readFile(VS_ID_FILE, "utf-8")).trim();
}

function buildPrompt(q) {
    // 入力 JSONL の形が多少違っても吸収する
    const meta = q.meta ?? {};
    const choices = normalizeChoices(q);
    const answer = q.answer ?? q.correct ?? q.correct_answer ?? q.correctChoice ?? q.correct_choice;

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

function getOutputText(resp) {
    // responses API の output_text 簡易取得（SDK 互換のために保険）
    if (typeof resp.output_text === "string") return resp.output_text;
    // fallback: output[...].content[...].text などを辿る
    try {
        const parts = [];
        for (const o of resp.output ?? []) {
            for (const c of o.content ?? []) {
                if (c.type === "output_text" && typeof c.text === "string") parts.push(c.text);
                if (c.type === "text" && typeof c.text === "string") parts.push(c.text);
            }
        }
        const s = parts.join("");
        return s || null;
    } catch {
        return null;
    }
}

async function callOpenAI(vsId, qObj) {
    // ここで “問題1問ぶんだけ” を投げる。巨大JSONをまとめて投げない。
    const req = {
        model: MODEL,
        store: false, // ログ保存不要なら false（任意）
        instructions: instructionsJa(),
        input: [
            {
                role: "user",
                content: [
                    {
                        type: "input_text",
                        text: JSON.stringify({
                            task: "explain_question",
                            question: qObj,
                            output_schema: {
                                explanation: "string",
                                law_citations: ["string"],
                            },
                        }),
                    },
                ],
            },
        ],
        // Vector Store への file_search
        tools: [
            {
                type: "file_search",
                vector_store_ids: [vsId],
                max_num_results: MAX_RESULTS,
            },
        ],
    };

    // GPT-5-mini / gpt-5 / gpt-5-nano は temperature 非対応なので送らない
    // gpt-5.2 / gpt-5.1 は reasoning.effort="none" のとき temperature 対応（デフォルト none）
    const isLegacyGpt5 = MODEL.startsWith("gpt-5") && !MODEL.startsWith("gpt-5.1") && !MODEL.startsWith("gpt-5.2");
    if (!isLegacyGpt5) {
        req.temperature = TEMPERATURE;
    }

    const resp = await client.responses.create(req);

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

    return { parsed, usage: resp.usage ?? null };
}

// 簡単なリトライ（429/5xx）
// ※この backoff は消さない。レート制限や一時障害で落ちるのを防ぐ。
async function withRetry(fn, { tries = 4, label = "" } = {}) {
    let lastErr;
    for (let i = 0; i < tries; i++) {
        try {
            OPENAI_STATS.attempts++;
            const ret = await fn();
            OPENAI_STATS.success++;
            return ret;
        } catch (e) {
            lastErr = e;
            const msg = String(e?.message ?? e);

            const isInsufficientQuota =
                msg.toLowerCase().includes("exceeded your current quota") ||
                msg.toLowerCase().includes("insufficient_quota");

            const isRetryable =
                !isInsufficientQuota && (
                    msg.includes("rate limit") ||
                    msg.includes("503") ||
                    msg.includes("502") ||
                    msg.includes("timeout") ||
                    msg.includes("ETIMEDOUT")
                );

            if (!isRetryable || i === tries - 1) throw e;

            OPENAI_STATS.retries++;
            const wait = 1500 * Math.pow(2, i);
            console.warn(`[retry] ${label} attempt=${i + 1}/${tries} wait_ms=${wait} msg=${msg}`);
            await new Promise((r) => setTimeout(r, wait));
        }
    }
    throw lastErr;
}

async function processOneBundle(vsId, bundlePath) {
    // --- デバッグ: 生成前に explanation 空件数を数える ---
    if (process.env.PRECOUNT_EMPTY === "1") {
        const tPre = Date.now();
        const { total, empty, parseError } = await countEmptyExplanationsInBundle(bundlePath);
        const msPre = Date.now() - tPre;
        console.log(
            `[pre] bundle=${path.basename(bundlePath)} total=${total} empty_explanation=${empty} ` +
            `parse_error=${parseError} ms=${msPre}`
        );
    }

    const tmpOut = bundlePath + ".tmp";
    const inStream = fs.createReadStream(bundlePath).pipe(zlib.createGunzip());
    const rl = readline.createInterface({ input: inStream, crlfDelay: Infinity });

    const gzip = zlib.createGzip({ level: 9 });
    const fileOut = fs.createWriteStream(tmpOut);
    gzip.pipe(fileOut);
    const outStream = gzip;

    // エラーを握りつぶさず落とす（Unhandled error event 防止）
    inStream.on("error", (e) => gzip.destroy(e));
    gzip.on("error", (e) => fileOut.destroy(e));
    fileOut.on("error", (e) => gzip.destroy(e));

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

        const qid =
            obj.id ??
            obj.qid ??
            obj.question_id ??
            obj.meta?.id ??
            `line${processed}`;

        // --- 先頭N件のスキップ確認ログ（skip側） ---
        if (DEBUG_FIRST_N > 0 && processed <= DEBUG_FIRST_N) {
            console.log(
                `[dbg] pre bundle=${path.basename(bundlePath)} idx=${processed} id=${qid} ` +
                `force=${FORCE} explanationEmpty=${isExplanationEmpty(obj)}`
            );
        }

        if (!FORCE && !isExplanationEmpty(obj)) {
            skipped++;

            // --- 先頭N件のスキップ確認ログ（skip確定） ---
            if (DEBUG_FIRST_N > 0 && processed <= DEBUG_FIRST_N) {
                console.log(
                    `[dbg] skip bundle=${path.basename(bundlePath)} idx=${processed} id=${qid} ` +
                    `reason=explanation_present`
                );
            }

            outStream.write(JSON.stringify(obj) + "\n");
            continue;
        }

        // --- 先頭N件の生成確認ログ（generate確定） ---
        if (DEBUG_FIRST_N > 0 && processed <= DEBUG_FIRST_N) {
            console.log(
                `[dbg] gen  bundle=${path.basename(bundlePath)} idx=${processed} id=${qid} ` +
                `reason=explanation_empty_or_force`
            );
        }

        const promptObj = buildPrompt(obj);

        const t0 = Date.now();
        const { parsed: result, usage } = await withRetry(() => callOpenAI(vsId, promptObj), {
            label: `${path.basename(bundlePath)}:${qid}`,
        });
        const ms = Date.now() - t0;

        obj.explanation = result.explanation;
        obj.law_citations = result.law_citations;

        OPENAI_STATS.total_ms += ms;
        if (usage) {
            const inTok = usage.input_tokens ?? 0;
            const outTok = usage.output_tokens ?? 0;
            const totTok = usage.total_tokens ?? (inTok + outTok);
            OPENAI_STATS.input_tokens += inTok;
            OPENAI_STATS.output_tokens += outTok;
            OPENAI_STATS.total_tokens += totTok;
        }

        if (LOG_PER_QUESTION) {
            if (usage) {
                console.log(
                    `[q] bundle=${path.basename(bundlePath)} id=${qid} ms=${ms} ` +
                    `tokens(in=${usage.input_tokens ?? "?"}, out=${usage.output_tokens ?? "?"}, total=${usage.total_tokens ?? "?"
                    })`,
                );
            } else {
                console.log(`[q] bundle=${path.basename(bundlePath)} id=${qid} ms=${ms} tokens(n/a)`);
            }
        }

        generated++;

        // 進捗ログ
        if (generated % 5 === 0) {
            console.log(`[${path.basename(bundlePath)}] generated=${generated} skipped=${skipped} processed=${processed}`);
        }

        outStream.write(JSON.stringify(obj) + "\n");
    }

    outStream.end();
    await once(fileOut, "close");

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

    let bundleFiles = await listBundleFiles(BUNDLES_DIR);

    // 1ファイルだけ処理したい場合（例: --bundle r03.jsonl.gz）
    if (ONLY_BUNDLE) {
        const byBase = path.basename(ONLY_BUNDLE);
        const targetAbs = path.resolve(path.isAbsolute(ONLY_BUNDLE) ? ONLY_BUNDLE : path.join(BUNDLES_DIR, ONLY_BUNDLE));
        bundleFiles = bundleFiles.filter((p) => path.resolve(p) === targetAbs || path.basename(p) === byBase);
    }

    if (bundleFiles.length === 0) {
        throw new Error(
            ONLY_BUNDLE ? `No bundle matched: ${ONLY_BUNDLE} (dir=${BUNDLES_DIR})` : `No bundle files found: ${BUNDLES_DIR}/*.jsonl.gz`,
        );
    }

    console.log(`Vector store: ${vsId}`);
    console.log(`Model       : ${MODEL}`);
    console.log(`Bundles     : ${bundleFiles.length}`);
    console.log(`Dry-run     : ${DRY_RUN}`);
    console.log(`Force       : ${FORCE}`);
    console.log(`Limit       : ${LIMIT}`);
    console.log(`Max-results : ${MAX_RESULTS}`);
    console.log(`Bundle      : ${ONLY_BUNDLE || "(all)"}`);
    console.log(`Log-per-q   : ${LOG_PER_QUESTION}`);

    let totalGen = 0;
    for (const b of bundleFiles) {
        const r = await processOneBundle(vsId, b);
        totalGen += r.generated;
    }

    if (!DRY_RUN) {
        await maybeUpdateManifest(DIST_DIR);
    }

    console.log(`Done. total_generated=${totalGen}`);

    const avgMs = OPENAI_STATS.success ? Math.round(OPENAI_STATS.total_ms / OPENAI_STATS.success) : 0;
    console.log(
        `[openai] attempts=${OPENAI_STATS.attempts} success=${OPENAI_STATS.success} retries=${OPENAI_STATS.retries} avg_ms=${avgMs} ` +
        `tokens_total=${OPENAI_STATS.total_tokens} (in=${OPENAI_STATS.input_tokens}, out=${OPENAI_STATS.output_tokens})`,
    );
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
