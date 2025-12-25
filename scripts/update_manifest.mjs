#!/usr/bin/env node
/**
 * dist/bundles/*.jsonl.gz をスキャンして manifest.json を再生成するスクリプト
 * 
 * Usage:
 *   node scripts/update_manifest.mjs [--dist ./dist]
 */

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import zlib from 'node:zlib';
import crypto from 'node:crypto';
import { pipeline } from 'node:stream/promises';

const args = process.argv.slice(2);
const getArg = (name, def) => {
    const i = args.indexOf(name);
    return i >= 0 && args[i + 1] ? args[i + 1] : def;
};

const DIST_DIR = getArg('--dist', 'dist');
const DIST_BASENAME = path.basename(path.resolve(DIST_DIR));
let BASE_URL = getArg('--base-url', DIST_BASENAME === 'dist' ? '' : `/${DIST_BASENAME}`);
if (BASE_URL) {
    if (!BASE_URL.startsWith('/')) BASE_URL = '/' + BASE_URL;
    if (BASE_URL.endsWith('/')) BASE_URL = BASE_URL.slice(0, -1);
}
const BUNDLES_DIR = path.join(DIST_DIR, 'bundles');
const MANIFEST_FILE = path.join(DIST_DIR, 'manifest.json');

// バージョン管理（簡易的）
const contentVersion = process.env.CONTENT_VERSION || new Date().toISOString().slice(0, 10).replace(/-/g, '.');
const generatedAt = new Date().toISOString();

async function getFileHashAndSize(filepath) {
    const buf = await fsp.readFile(filepath);
    const hash = crypto.createHash('sha256').update(buf).digest('hex');
    return { size: buf.length, sha256: hash };
}

async function readFirstLineMetadata(filepath) {
    // 最初の1行(JSON)だけ読んでメタデータを取得する
    // 同時に行数(=アイテム数)もカウントする

    // Gzipを行単位で読むのは少し面倒だが、ストリームで処理する
    const fileStream = fs.createReadStream(filepath);
    const unzip = zlib.createGunzip();

    let firstLine = '';
    let lineCount = 0;

    // readline相当の簡易実装
    // 全部展開してメモリに乗るサイズなら良いが、念のためChunkで処理...
    // 今回はjsonl.gz全体を展開してもメモリに乗る前提だが、
    // 安全のためストリーム消費でカウントする

    return new Promise((resolve, reject) => {
        fileStream.on('error', reject);
        unzip.on('error', reject);

        let buffer = '';
        let headerFound = false;
        let headerObj = null;

        const stream = fileStream.pipe(unzip);

        stream.on('data', (chunk) => {
            buffer += chunk.toString('utf-8');

            // 行を探す
            let lineEnd;
            while ((lineEnd = buffer.indexOf('\n')) !== -1) {
                const line = buffer.slice(0, lineEnd).trim();
                buffer = buffer.slice(lineEnd + 1);

                if (line) {
                    lineCount++;
                    if (!headerFound) {
                        try {
                            headerObj = JSON.parse(line);
                            headerFound = true;
                        } catch (e) {
                            // ignore check
                        }
                    }
                }
            }
        });

        stream.on('end', () => {
            // 残りバッファ
            if (buffer.trim()) {
                lineCount++;
                if (!headerFound) {
                    try {
                        headerObj = JSON.parse(buffer.trim());
                    } catch { }
                }
            }
            resolve({ header: headerObj, count: lineCount });
        });
    });
}

function toTitle(era, eraYear, items) {
    const left = era && eraYear ? `${era}${eraYear}年` : '';
    return `${left} 全${items}問`.trim();
}

function latestUpdatedAt(dummyItems) {
    // バンドル内の全データをなめるのはコストが高いので、
    // ここでは現在時刻 or manifest生成時刻をデフォルトとする。
    // もし厳密にやりたければ readFirstLineMetadata で updated_at の最大値を探す必要がある。
    // 今回は簡易的に現在時刻を利用（再生成＝メタデータ更新の意図も含むため）
    return new Date().toISOString();
}

async function main() {
    console.log(`Scanning bundles in: ${BUNDLES_DIR}`);
    if (!fs.existsSync(BUNDLES_DIR)) {
        console.error(`Error: Bundles dir not found: ${BUNDLES_DIR}`);
        process.exit(1);
    }

    const files = (await fsp.readdir(BUNDLES_DIR))
        .filter(f => f.endsWith('.jsonl.gz'))
        .sort();

    const bundles = [];

    for (const f of files) {
        const fullPath = path.join(BUNDLES_DIR, f);
        console.log(`Processing ${f}...`);

        const { size, sha256 } = await getFileHashAndSize(fullPath);
        const { header, count } = await readFirstLineMetadata(fullPath);

        if (!header) {
            console.warn(`Warning: Could not read JSON header from ${f}. Skipping.`);
            continue;
        }

        const id = path.basename(f, '.jsonl.gz'); // r07
        const yy = header.year || 0; // 2025

        const entry = {
            id: id,
            title: toTitle(header.era, header.era_year, count),
            year: Number(yy),
            items: count,
            url: `${BASE_URL}/bundles/${f}`,
            size: size,
            sha256: sha256,
            etag: `W/"${id}@${contentVersion}"`,
            updated_at: generatedAt // 強制refresh
        };

        bundles.push(entry);
    }

    const manifest = {
        schema_version: '1.1.0',
        content_version: contentVersion,
        generated_at: generatedAt,
        bundles
    };

    await fsp.writeFile(MANIFEST_FILE, JSON.stringify(manifest, null, 2), 'utf-8');
    console.log(`\nUpdated ${MANIFEST_FILE}`);
    console.log(`Bundles: ${bundles.length} files processed.`);
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
