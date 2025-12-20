// scripts/openai_sync_vector_store.mjs
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import OpenAI from "openai";

// ---------------------------
// Config
// ---------------------------
const DEFAULT_ROOT = "laws_index"; // txt出力先ルート
const STATE_DIR = ".openai";
const VS_ID_FILE = path.join(STATE_DIR, "vector_store_id.txt");
const STATE_FILE = path.join(STATE_DIR, "vector_store_sync_state.json");

// CLI args
const args = new Set(process.argv.slice(2));
const getArgValue = (key) => {
    const i = process.argv.indexOf(key);
    return i >= 0 ? process.argv[i + 1] : undefined;
};

const ROOT_DIR = getArgValue("--root") ?? DEFAULT_ROOT;
const DATE_DIR = getArgValue("--date"); // 例: 2024-09-01（未指定なら最新日付を選ぶ）
const DRY_RUN = args.has("--dry-run");
const PRUNE = args.has("--prune"); // ローカルに無いファイルをvector storeから外し、Filesも削除

const VS_NAME =
    process.env.OPENAI_VECTOR_STORE_NAME ??
    "RealEstateAppraiserLaws (laws_index)";

const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

// openai-node の世代差吸収（beta経由のことがある）
const vectorStores = openai.vectorStores ?? openai.beta?.vectorStores;
if (!vectorStores) {
    throw new Error(
        "OpenAI SDK does not expose vectorStores. Please update the 'openai' package."
    );
}

// ---------------------------
// Utilities
// ---------------------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureDir(p) {
    await fsp.mkdir(p, { recursive: true });
}

async function fileExists(p) {
    try {
        await fsp.access(p, fs.constants.F_OK);
        return true;
    } catch {
        return false;
    }
}

async function listDateFolders(root) {
    const entries = await fsp.readdir(root, { withFileTypes: true });
    return entries
        .filter((e) => e.isDirectory())
        .map((e) => e.name)
        .filter((name) => /^\d{4}-\d{2}-\d{2}$/.test(name))
        .sort(); // 昇順
}

async function pickLatestDateFolder(root) {
    const dates = await listDateFolders(root);
    if (dates.length === 0) {
        throw new Error(`No YYYY-MM-DD folders found under: ${root}`);
    }
    return dates[dates.length - 1];
}

async function walkFiles(dir, exts = new Set([".txt"])) {
    const out = [];
    const stack = [dir];
    while (stack.length) {
        const cur = stack.pop();
        const entries = await fsp.readdir(cur, { withFileTypes: true });
        for (const e of entries) {
            const p = path.join(cur, e.name);
            if (e.isDirectory()) stack.push(p);
            else if (e.isFile() && exts.has(path.extname(e.name).toLowerCase()))
                out.push(p);
        }
    }
    out.sort();
    return out;
}

async function sha256OfFile(filePath) {
    return new Promise((resolve, reject) => {
        const h = crypto.createHash("sha256");
        const s = fs.createReadStream(filePath);
        s.on("data", (chunk) => h.update(chunk));
        s.on("end", () => resolve(h.digest("hex")));
        s.on("error", reject);
    });
}

async function readJson(p, fallback) {
    if (!(await fileExists(p))) return fallback;
    const raw = await fsp.readFile(p, "utf-8");
    return JSON.parse(raw);
}

async function writeJson(p, obj) {
    await ensureDir(path.dirname(p));
    await fsp.writeFile(p, JSON.stringify(obj, null, 2) + "\n", "utf-8");
}

async function readText(p) {
    if (!(await fileExists(p))) return null;
    return (await fsp.readFile(p, "utf-8")).trim() || null;
}

async function writeText(p, s) {
    await ensureDir(path.dirname(p));
    await fsp.writeFile(p, s + "\n", "utf-8");
}

// SDK世代差：del / delete の両対応
async function deleteUnderlyingFile(fileId) {
    if (typeof openai.files?.del === "function") return openai.files.del(fileId);
    if (typeof openai.files?.delete === "function") return openai.files.delete(fileId);
    throw new Error("SDK does not expose files.del or files.delete");
}

async function deleteVectorStoreFile(vsId, fileId) {
    // vector store file の削除は「vector store から外す」動作 :contentReference[oaicite:4]{index=4}
    const vsFiles = vectorStores.files;
    if (typeof vsFiles?.del === "function") return vsFiles.del(vsId, fileId);
    if (typeof vsFiles?.delete === "function") return vsFiles.delete(vsId, fileId);
    throw new Error("SDK does not expose vectorStores.files.del or delete");
}

// file batch create + poll（createAndPollがあればそれを使い、無ければ手動poll）
async function createFileBatchAndPoll(vsId, fileIdsOrFiles) {
    const fb = vectorStores.fileBatches;

    // 公式ガイドは「create and poll helpers」を推奨 :contentReference[oaicite:5]{index=5}
    if (typeof fb?.createAndPoll === "function") {
        return fb.createAndPoll(vsId, fileIdsOrFiles);
    }

    // 手動poll: API上は status を completed まで待つ :contentReference[oaicite:6]{index=6}
    const created = await fb.create(vsId, fileIdsOrFiles);
    const batchId = created.id;

    for (; ;) {
        const cur = await fb.retrieve(vsId, batchId);
        if (cur.status === "completed") return cur;
        if (cur.status === "failed" || cur.status === "cancelled") {
            throw new Error(
                `Vector store file batch ${batchId} ended with status=${cur.status}: ` +
                JSON.stringify(cur.file_counts ?? {}, null, 2)
            );
        }
        await sleep(4000);
    }
}

// simple concurrency pool
async function mapLimit(items, limit, fn) {
    const ret = [];
    let i = 0;
    const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
        while (i < items.length) {
            const idx = i++;
            ret[idx] = await fn(items[idx], idx);
        }
    });
    await Promise.all(workers);
    return ret;
}

// ---------------------------
// Main
// ---------------------------
async function main() {
    if (!process.env.OPENAI_API_KEY) {
        throw new Error("OPENAI_API_KEY is required.");
    }

    const date = DATE_DIR ?? (await pickLatestDateFolder(ROOT_DIR));
    const targetDir = path.join(ROOT_DIR, date);

    if (!(await fileExists(targetDir))) {
        throw new Error(`Target dir not found: ${targetDir}`);
    }

    // vector store id を決める（env優先、無ければファイルから読む、無ければ作る）
    await ensureDir(STATE_DIR);
    let vsId =
        process.env.OPENAI_VECTOR_STORE_ID ??
        (await readText(VS_ID_FILE));

    if (!vsId) {
        if (DRY_RUN) {
            console.log(`[dry-run] Would create vector store: ${VS_NAME}`);
            vsId = "vs_dry_run";
        } else {
            const vs = await vectorStores.create({
                name: VS_NAME,
                // expires_after を入れると「最後に使ってからN日」で自動失効にできる（コスト管理） :contentReference[oaicite:7]{index=7}
                // expires_after: { anchor: "last_active_at", days: 365 },
            });
            vsId = vs.id;
            await writeText(VS_ID_FILE, vsId);
        }
    }

    const state = await readJson(STATE_FILE, {
        version: 1,
        root: targetDir,
        files: {}, // relPath -> { sha256, openai_file_id, uploaded_at }
    });

    const files = await walkFiles(targetDir, new Set([".txt"]));
    if (files.length === 0) {
        console.log(`No .txt files found under ${targetDir}`);
        return;
    }

    const rel = (p) => path.relative(targetDir, p).replaceAll("\\", "/");
    const currentRelSet = new Set(files.map(rel));

    // prune対象（ローカルに無いのにstateに残っている）
    const stale = Object.keys(state.files).filter((r) => !currentRelSet.has(r));

    // 変更検出
    console.log(`Vector store: ${vsId}`);
    console.log(`Target dir : ${targetDir}`);
    console.log(`Files      : ${files.length}`);

    const planned = [];
    for (const fp of files) {
        const r = rel(fp);
        const sha = await sha256OfFile(fp);
        const prev = state.files[r];

        if (!prev || prev.sha256 !== sha) {
            planned.push({ fp, r, sha, prevFileId: prev?.openai_file_id ?? null });
        }
    }

    console.log(`To upload  : ${planned.length}`);

    // 1) 変更分を Files API にアップロード（purpose: assistants） :contentReference[oaicite:8]{index=8}
    const uploads = await mapLimit(planned, 3, async (item) => {
        if (DRY_RUN) {
            return { ...item, newFileId: `file_dry_${item.r}` };
        }

        // 変更がある場合、古いファイルを「vector storeから外す＋Files削除」（重複防止）
        // vector store file delete: vector store から外す :contentReference[oaicite:9]{index=9}
        // file delete: underlying file を削除 :contentReference[oaicite:10]{index=10}
        if (item.prevFileId && PRUNE) {
            try {
                await deleteVectorStoreFile(vsId, item.prevFileId);
            } catch (e) {
                // 既に外れてる等は許容
                console.warn(`warn: failed to detach old file from vector store: ${item.prevFileId}`);
            }
            try {
                await deleteUnderlyingFile(item.prevFileId);
            } catch (e) {
                console.warn(`warn: failed to delete old file: ${item.prevFileId}`);
            }
        }

        const uploaded = await openai.files.create({
            file: fs.createReadStream(item.fp),
            purpose: "assistants",
        });

        return { ...item, newFileId: uploaded.id };
    });

    const newFileIds = uploads.map((u) => u.newFileId);

    // 2) vector store に batch で紐付け（最大500件ずつ） :contentReference[oaicite:11]{index=11}
    if (newFileIds.length > 0) {
        // attributes を付けたい場合は file_ids ではなく files 配列を使う :contentReference[oaicite:12]{index=12}
        // ここでは “relPath” を attributes に入れておく（後でトレースやデバッグに便利）
        const batchPayload = {
            files: uploads.map((u) => ({
                file_id: u.newFileId,
                attributes: {
                    rel_path: u.r,
                    law_date: date,
                },
            })),
        };

        if (DRY_RUN) {
            console.log(`[dry-run] Would create file batch with ${uploads.length} files`);
        } else {
            // 500件超えると分割
            const chunkSize = 500;
            for (let i = 0; i < batchPayload.files.length; i += chunkSize) {
                const chunk = batchPayload.files.slice(i, i + chunkSize);
                console.log(`Attaching to vector store... (${i + 1}-${i + chunk.length})`);
                const res = await createFileBatchAndPoll(vsId, { files: chunk });
                console.log(
                    `Batch status=${res.status} counts=${JSON.stringify(res.file_counts ?? {})}`
                );
            }
        }
    }

    // state更新
    const now = new Date().toISOString();
    for (const u of uploads) {
        state.files[u.r] = {
            sha256: u.sha,
            openai_file_id: u.newFileId,
            uploaded_at: now,
        };
    }
    state.root = targetDir;

    // 3) prune（ローカルから消えたものを外す）
    if (PRUNE && stale.length > 0) {
        console.log(`Prune      : ${stale.length}`);
        if (!DRY_RUN) {
            for (const r of stale) {
                const oldId = state.files[r]?.openai_file_id;
                if (!oldId) continue;

                try {
                    await deleteVectorStoreFile(vsId, oldId);
                } catch (e) {
                    console.warn(`warn: failed to detach stale file from vector store: ${oldId}`);
                }

                try {
                    await deleteUnderlyingFile(oldId);
                } catch (e) {
                    console.warn(`warn: failed to delete stale file: ${oldId}`);
                }

                delete state.files[r];
            }
        }
    }

    await writeJson(STATE_FILE, state);

    console.log("Done.");
    console.log(`vector_store_id=${vsId}`);
    console.log(`state=${STATE_FILE}`);
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
