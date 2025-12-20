#!/usr/bin/env node
/**
 * e-Gov 法令XML (laws/<date>/*.xml) → 検索向けの .txt (laws_index/<date>/*.txt)
 *
 * 1法令=1txt にまとめる版（ファイル数爆増を避ける）
 *
 * Usage:
 *   node scripts/laws_xml_to_txt.mjs                # laws/ 配下の最新日付フォルダを自動選択
 *   node scripts/laws_xml_to_txt.mjs 2024-09-01     # 明示
 */

import fs from "node:fs";
import path from "node:path";
import { XMLParser } from "fast-xml-parser";

const REPO_ROOT = process.cwd();
const LAWS_ROOT = path.join(REPO_ROOT, "laws");
const OUT_ROOT = path.join(REPO_ROOT, "laws_index");

const parser = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@_",
    textNodeName: "#text",
    // e-Gov XMLは改行/空白が多いので trim しすぎない
    trimValues: true,
});

function normalizeArray(v) {
    if (v == null) return [];
    return Array.isArray(v) ? v : [v];
}

function safeText(v) {
    if (v == null) return "";
    if (typeof v === "string") return v.trim();
    if (typeof v === "number") return String(v);
    if (typeof v === "object") {
        if (typeof v["#text"] === "string") return v["#text"].trim();
    }
    return "";
}

function listSubdirs(dir) {
    if (!fs.existsSync(dir)) return [];
    return fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((d) => d.isDirectory())
        .map((d) => d.name)
        .sort();
}

function pickLatestDateDir() {
    const sub = listSubdirs(LAWS_ROOT);
    if (sub.length === 0) throw new Error(`No date directories under ${LAWS_ROOT}`);
    // YYYY-MM-DD の辞書順 = 時系列順
    return sub[sub.length - 1];
}

function walkFiles(dir, ext = ".xml") {
    const out = [];
    const stack = [dir];
    while (stack.length) {
        const cur = stack.pop();
        for (const ent of fs.readdirSync(cur, { withFileTypes: true })) {
            const p = path.join(cur, ent.name);
            if (ent.isDirectory()) stack.push(p);
            else if (ent.isFile() && p.toLowerCase().endsWith(ext)) out.push(p);
        }
    }
    out.sort();
    return out;
}

/**
 * MainProvision 配下のどこに居ても Article を回収（Chapter 等の入れ子対応）
 */
function collectArticles(mainProvisionObj) {
    const found = [];

    function visit(node) {
        if (node == null) return;
        if (Array.isArray(node)) {
            for (const x of node) visit(x);
            return;
        }
        if (typeof node !== "object") return;

        // "Article" キーがあれば回収
        if (node.Article) {
            for (const a of normalizeArray(node.Article)) found.push(a);
        }

        // 子を再帰
        for (const k of Object.keys(node)) {
            visit(node[k]);
        }
    }

    visit(mainProvisionObj);
    return found;
}

/**
 * Paragraph / Item / Subitem の Sentence を拾って “読みやすい条文テキスト” を作る
 * （完全な順序再現までは狙わず、RAG検索の安定性と参照性を優先）
 */
function renderSentenceContainer(container) {
    const sentences = [];

    function pushSentenceNode(sn) {
        // Sentence は "string" または {"#text": "...", "@_Num": "..."} の場合がある
        const t = safeText(sn);
        if (t) sentences.push(t);
    }

    // ParagraphSentence
    if (container?.ParagraphSentence?.Sentence) {
        for (const sn of normalizeArray(container.ParagraphSentence.Sentence)) pushSentenceNode(sn);
    }

    // Item / Subitem（ItemSentence / Subitem1Sentence など）
    // Item
    if (container?.Item) {
        for (const item of normalizeArray(container.Item)) {
            const itemTitle = safeText(item.ItemTitle);
            const itemSentences = [];
            if (item?.ItemSentence?.Sentence) {
                for (const sn of normalizeArray(item.ItemSentence.Sentence)) {
                    const t = safeText(sn);
                    if (t) itemSentences.push(t);
                }
            }
            if (itemTitle || itemSentences.length) {
                sentences.push(`\n  ${itemTitle || ""}`.trimEnd());
                if (itemSentences.length) sentences.push(`  ${itemSentences.join("")}`);
            }

            // Subitem1（号の下の(イ)(ロ)みたいな枝）
            if (item.Subitem1) {
                for (const sub of normalizeArray(item.Subitem1)) {
                    const subTitle = safeText(sub.Subitem1Title);
                    const subSentences = [];
                    if (sub?.Subitem1Sentence?.Sentence) {
                        for (const sn of normalizeArray(sub.Subitem1Sentence.Sentence)) {
                            const t = safeText(sn);
                            if (t) subSentences.push(t);
                        }
                    }
                    if (subTitle || subSentences.length) {
                        sentences.push(`\n    ${subTitle || ""}`.trimEnd());
                        if (subSentences.length) sentences.push(`    ${subSentences.join("")}`);
                    }
                }
            }
        }
    }

    return sentences.join("");
}

function renderArticle(articleObj) {
    const numAttr = articleObj?.["@_Num"] ?? "";
    const articleTitle = safeText(articleObj.ArticleTitle); // 例: "第一条"
    const caption = safeText(articleObj.ArticleCaption);    // 例: "（目的）"

    const headerParts = [];
    if (articleTitle) headerParts.push(articleTitle);
    // 検索ヒット率のため "第1条" も併記（任意）
    if (numAttr) headerParts.push(`(第${numAttr}条)`);
    if (caption) headerParts.push(caption);

    const lines = [];
    lines.push(`## ${headerParts.join(" ")}`.trim());

    for (const p of normalizeArray(articleObj.Paragraph)) {
        const pNum = p?.["@_Num"]; // "1","2",...
        // 第1項は空欄のことが多いのでラベル省略
        const pLabel = pNum && pNum !== "1" ? `（第${pNum}項）` : "";
        const body = renderSentenceContainer(p);
        const text = (pLabel ? `${pLabel}\n` : "") + body;
        if (text.trim()) lines.push(text.trim());
    }

    return lines.join("\n") + "\n";
}

function guessLawIdFromFilename(filePath) {
    // e-Govのファイル名: <日本語名>_414AC0000000078_2025....xml みたいなことが多い
    const base = path.basename(filePath);
    const m = base.match(/_([0-9A-Z]{15,})_/); // ざっくり：英数15+をID扱い
    return m ? m[1] : base.replace(/\.xml$/i, "");
}

function buildLawTxt(xmlText, srcPath) {
    const doc = parser.parse(xmlText);
    const law = doc?.Law;
    if (!law) throw new Error("Invalid XML: missing <Law>");

    const lawNum = safeText(law.LawNum); // 例: 平成十四年法律第七十八号
    const lawBody = law.LawBody ?? {};
    const lawTitleNode = lawBody.LawTitle ?? {};
    const lawTitle = safeText(lawTitleNode); // 例: マンションの建替え等の円滑化に関する法律
    const abbrev = lawTitleNode?.["@_Abbrev"] ? String(lawTitleNode["@_Abbrev"]).trim() : "";

    const mainProvision = lawBody.MainProvision;
    if (!mainProvision) throw new Error("Invalid XML: missing LawBody/MainProvision");

    const articles = collectArticles(mainProvision);
    const lawId = guessLawIdFromFilename(srcPath);

    const out = [];
    out.push(`# ${lawTitle || "(法令名不明)"}${abbrev ? `（略称：${abbrev}）` : ""}`.trim());
    out.push(`- 法令番号: ${lawNum || "(不明)"}`);
    out.push(`- law_id: ${lawId}`);
    out.push("");
    out.push("## 本文");
    out.push("");

    for (const a of articles) {
        out.push(renderArticle(a));
    }

    return out.join("\n").replace(/\n{3,}/g, "\n\n");
}

function main() {
    const dateDir = process.argv[2] ?? pickLatestDateDir();
    const inDir = path.join(LAWS_ROOT, dateDir);
    const outDir = path.join(OUT_ROOT, dateDir);

    if (!fs.existsSync(inDir)) {
        throw new Error(`Input dir not found: ${inDir}`);
    }
    fs.mkdirSync(outDir, { recursive: true });

    const files = walkFiles(inDir, ".xml");
    if (files.length === 0) {
        throw new Error(`No XML files under: ${inDir}`);
    }

    console.log(`[laws_xml_to_txt] input=${inDir} xml_files=${files.length}`);
    console.log(`[laws_xml_to_txt] output=${outDir}`);

    for (const f of files) {
        const xml = fs.readFileSync(f, "utf-8");
        const txt = buildLawTxt(xml, f);

        const lawId = guessLawIdFromFilename(f);
        const outPath = path.join(outDir, `${lawId}.txt`);
        fs.writeFileSync(outPath, txt, "utf-8");
    }

    console.log("[laws_xml_to_txt] done");
}

main();
