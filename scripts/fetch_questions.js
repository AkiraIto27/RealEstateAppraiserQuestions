/**
 * 国交省の不動産鑑定士試験ページから過去問・正解のPDFをダウンロードするスクリプト
 * Usage: node scripts/fetch_questions.js [--year 2024]
 */
import fs from 'node:fs';
import path from 'node:path';
import { Readable } from 'node:stream';
import { finished } from 'node:stream/promises';
import * as cheerio from 'cheerio';

const TARGET_URL = 'https://www.mlit.go.jp/totikensangyo/kanteishi/shiken02.html';
const RAW_DATA_DIR = './raw_data';

// 引数解析
const args = process.argv.slice(2);
const yearArg = args.find(a => a.startsWith('--year='))?.split('=')[1] || args[args.indexOf('--year') + 1];

async function downloadFile(url, destPath) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status} ${res.statusText}`);

    // ディレクトリ作成
    fs.mkdirSync(path.dirname(destPath), { recursive: true });

    const fileStream = fs.createWriteStream(destPath);
    await finished(Readable.fromWeb(res.body).pipe(fileStream));
}

// 和暦→西暦変換（簡易版）
function convertEraToYear(text) {
    const m = text.match(/(令和|平成)(\d+|元)年/);
    if (!m) return null;
    const era = m[1];
    let eraYear = m[2] === '元' ? 1 : Number(m[2]);

    if (era === '令和') return 2018 + eraYear;
    if (era === '平成') return 1988 + eraYear;
    return null;
}

// 全角数字を半角に
function toHalfWidth(str) {
    return str.replace(/[０-９]/g, s => String.fromCharCode(s.charCodeAt(0) - 0xFEE0));
}

async function main() {
    console.log(`Fetching ${TARGET_URL}...`);
    const res = await fetch(TARGET_URL);
    if (!res.ok) throw new Error(`Status ${res.status}`);
    const html = await res.text();
    const $ = cheerio.load(html);

    // 構造解析
    // "○ 令和○年不動産鑑定士試験" というテキストを含む要素を探す
    // その後の要素に含まれる "不動産に関する行政法規" -> "問 題" などを探す

    // データ構造: year -> { gyousei: { q, a }, kantei: { q, a } }
    const exams = [];

    // メインコンテンツ内のテキストを走査
    // MLITの構造は少し複雑なので、特定のキーワードを含む行を探して、その周辺のリンクを取得するアプローチをとる

    // "○ 令和〜" のような見出しを探す
    // 実例: <p>○ 令和７年不動産鑑定士試験<br> ... </p> というパターンが多いが、
    // テキストノードで分割されている可能性もある。

    // body内の全テキストノードを見るのは重いので、メインエリアっぽい場所を絞る
    // class="article-body" や main contents

    // 全体のテキストを正規化して構造化を試みる
    // リスト構造ではないようなので、線形にスキャンする

    const bodyText = $('body').text();
    // HTML構造に依存しすぎないように、Cheerioで特定のキーワードを持つ要素を起点にする

    // "○ 令和" を含む要素を全て取得
    const yearHeaders = $('*').filter((i, el) => {
        const t = $(el).text().trim();
        return /^○\s*(令和|平成)[０-９\d]+年不動産鑑定士試験/.test(t) && $(el).children().length === 0; // 末端要素のみ
    });

    // 上記だと「末端要素」が取れない場合（<p>○ 令和...<br>...<a...>...</a></p> のように混在している場合）
    // 親要素を取得してパースしたほうが安全

    // "短答式試験" のセクションを探す
    // H2やH3などを探す

    console.log('Parsing page structure...');

    // 戦略変更: "不動産に関する行政法規" というテキストを持つ要素の近くにある "問 題" リンクを探す
    // これを年度ごとにグルーピングする

    // 親要素（年単位のブロック）を特定するのがカギ
    // MLITのページは <p> や <div> で区切られていることが多い

    // 実際にブラウザエージェントが見つけた構造に近い形を探す
    // user request:
    // ○ 令和７年不動産鑑定士試験
    // 　不動産に関する行政法規	 問 題 	 正 解 	答案用紙

    // これらが1つの <p> に入っているか、隣接しているか。
    // HTMLソースを見ないと確定できないが、cheerioで緩く探す。

    // 年度の抽出
    // 全ての <a> タグを洗い出し、その前にある "令和〜年" を探すのは非効率。

    // テキストベースで分割して解析する
    const textLines = [];

    // 再帰的にテキストとリンクを抽出
    function extractTextAndLinks(elem) {
        if (elem.type === 'text') {
            const t = elem.data.trim();
            if (t) textLines.push({ type: 'text', text: toHalfWidth(t) });
        } else if (elem.type === 'tag' && elem.name === 'a') {
            const t = $(elem).text().trim();
            const href = $(elem).attr('href');
            if (t && href) textLines.push({ type: 'link', text: toHalfWidth(t), href });
        } else if (elem.children) {
            elem.children.forEach(c => extractTextAndLinks(c));
        }
    }

    // main body だけに絞る
    const contentArea = $('.kanteishi-shiken02') // クラス名があれば
    if (contentArea.length) {
        extractTextAndLinks(contentArea[0]);
    } else {
        extractTextAndLinks($('body')[0]);
    }

    // 線形スキャンでステートマシン的に処理
    let currentYear = null;
    let currentData = {};

    const results = {}; // year -> { gyousei: {q, a}, kantei: {q, a} }

    for (let i = 0; i < textLines.length; i++) {
        const item = textLines[i];

        if (item.type === 'text') {
            const m = item.text.match(/○\s*(令和(\d+|元))年不動産鑑定士試験/);
            if (m) {
                currentYear = convertEraToYear(`令和${m[2]}年`);
                if (!results[currentYear]) {
                    results[currentYear] = {
                        gyousei: { q: null, a: null },
                        kantei: { q: null, a: null }
                    };
                }
                // console.log(`Found Year: ${currentYear}`);
                continue;
            }
        }

        if (!currentYear) continue;

        // 科目の判定（直近のテキストを見る）
        // "不動産に関する行政法規" の直後のリンクを探す

        // 簡易ロジック:
        // "問 題" リンクを見つけたら、その直前のテキストで科目を判定

        if (item.type === 'link') {
            const txt = item.text.replace(/\s+/g, '');
            if (txt === '問題' || txt === '正解') {
                // リンクの場合、その前の要素（テキスト）を遡って科目を特定する
                let subject = null;
                for (let j = i - 1; j >= 0; j--) {
                    if (textLines[j].type === 'text') {
                        const t = textLines[j].text;
                        if (t.includes('行政法規')) {
                            subject = 'gyousei';
                            break;
                        }
                        if (t.includes('鑑定評価') || t.includes('理論')) { // "不動産の鑑定評価に関する理論"
                            subject = 'kantei';
                            break;
                        }
                        // 別の年度まで戻ってしまったらアウト
                        if (t.includes('○')) break;
                    }
                }

                if (subject) {
                    const type = txt === '問題' ? 'q' : 'a';
                    const fullUrl = new URL(item.href, TARGET_URL).toString();
                    results[currentYear][subject][type] = fullUrl;
                    // console.log(`  Set ${currentYear} ${subject} ${type} -> ${fullUrl}`);
                }
            }
        }
    }

    console.log('Found exams:', Object.keys(results).sort());

    // ダウンロード対象の年度
    let targetYear = yearArg ? Number(yearArg) : Math.max(...Object.keys(results).map(Number));
    if (!results[targetYear]) {
        console.error(`Year ${targetYear} not found.`);
        console.log('Available:', Object.keys(results));
        return;
    }

    console.log(`Targeting Year: ${targetYear}`);
    const data = results[targetYear];

    const downloads = [];
    if (data.gyousei.q) downloads.push({ url: data.gyousei.q, path: path.join(RAW_DATA_DIR, `${targetYear}`, 'gyousei_question.pdf') });
    if (data.gyousei.a) downloads.push({ url: data.gyousei.a, path: path.join(RAW_DATA_DIR, `${targetYear}`, 'gyousei_answer.pdf') });
    if (data.kantei.q) downloads.push({ url: data.kantei.q, path: path.join(RAW_DATA_DIR, `${targetYear}`, 'kantei_question.pdf') });
    if (data.kantei.a) downloads.push({ url: data.kantei.a, path: path.join(RAW_DATA_DIR, `${targetYear}`, 'kantei_answer.pdf') });

    if (downloads.length === 0) {
        console.warn('No PDF links found for this year.');
        return;
    }

    for (const d of downloads) {
        console.log(`Downloading ${path.basename(d.path)}...`);
        try {
            await downloadFile(d.url, d.path);
        } catch (e) {
            console.error(`Error downloading ${d.url}:`, e.message);
        }
    }

    console.log('Download complete.');
}

main().catch(e => {
    console.error(e);
    process.exit(1);
});
