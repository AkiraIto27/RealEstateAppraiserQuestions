/**
 * PDFをGemini 1.5 Proに投げてCSV化するスクリプト
 * Usage: node scripts/pdf_to_csv.mjs [--year 2025]
 */
import fs from 'node:fs';
import path from 'node:path';
import { GoogleGenerativeAI } from '@google/generative-ai';
import { GoogleAIFileManager } from '@google/generative-ai/server';

// Load env
try {
    await import('dotenv/config');
} catch {
    // ignore
}

// -------------------------------------------------------------
// Config
// -------------------------------------------------------------
const RAW_DATA_DIR = './raw_data';
const DATA_DIR = './data';
const API_KEY = process.env.GEMINI_API_KEY;

if (!API_KEY) {
    console.error('Error: GEMINI_API_KEY is required.');
    process.exit(1);
}

const fileManager = new GoogleAIFileManager(API_KEY);
const genAI = new GoogleGenerativeAI(API_KEY);
const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });

// 引数解析
const args = process.argv.slice(2);
const yearArg = args.find(a => a.startsWith('--year='))?.split('=')[1] || args[args.indexOf('--year') + 1];

// -------------------------------------------------------------
// Helpers
// -------------------------------------------------------------

async function uploadFile(filePath, mimeType) {
    console.log(`Uploading ${filePath}...`);
    const uploadResult = await fileManager.uploadFile(filePath, {
        mimeType,
        displayName: path.basename(filePath),
    });
    const file = uploadResult.file;
    console.log(`Uploaded file ${file.displayName} as: ${file.uri}`);
    return file;
}

async function waitForFileActive(file) {
    let currentFile = await fileManager.getFile(file.name);
    while (currentFile.state === "PROCESSING") {
        process.stdout.write(".");
        await new Promise((resolve) => setTimeout(resolve, 2000));
        currentFile = await fileManager.getFile(file.name);
    }
    if (currentFile.state !== "ACTIVE") {
        throw new Error(`File ${file.name} failed to process`);
    }
    console.log('File is ready.');
    return currentFile;
}

async function extractQuestions(pdfUri, subjectName) {
    console.log(`Extracting questions for ${subjectName}...`);
    const prompt = `
あなたは有能なデータ入力アシスタントです。
提供された不動産鑑定士試験（${subjectName}）の問題PDFから、全ての「問題」を抽出してJSON形式で出力してください。

## 出力仕様
- JSONのみを出力すること。Markdownのコードブロックは不要。
- 配列形式:
[
  {
    "question_no": 1,
    "statement": "問題文のテキスト...",
    "choice1": "選択肢1のテキスト",
    "choice2": "選択肢2のテキスト",
    "choice3": "...",
    "choice4": "...",
    "choice5": "..."
  },
  ...
]

## 注意点
- 全ての問題（通常40問）を漏らさず抽出すること。
- 問題文（statement）に含まれる改行は削除せず、そのまま維持すること。
- 図表が含まれる問題でテキスト化できない場合は "[図表あり]" と記載すること。
- 選択肢が「正しいものはいくつあるか」形式の場合、ア・イ・ウ・エなどの記述もそれぞれの選択肢として適切に整形すること（ただしCSVのカラムはchoice1~5なので、ア〜オを1〜5にマッピングする）。
`;

    try {
        const result = await model.generateContent([
            { fileData: { mimeType: "application/pdf", fileUri: pdfUri } },
            { text: prompt }
        ]);
        const text = result.response.text();
        return parseJson(text);
    } catch (e) {
        console.error('Gemini extraction failed:', e);
        return [];
    }
}

async function extractAnswers(pdfUri) {
    console.log(`Extracting answers...`);
    const prompt = `
提供された正解PDFから、問題番号と正解番号のペアを抽出してJSON形式で出力してください。

## 出力仕様
- JSON配列形式:
[
  { "question_no": 1, "answer": 3 },
  ...
]
`;
    try {
        const result = await model.generateContent([
            { fileData: { mimeType: "application/pdf", fileUri: pdfUri } },
            { text: prompt }
        ]);
        const text = result.response.text();
        return parseJson(text);
    } catch (e) {
        console.error('Gemini answer extraction failed:', e);
        return [];
    }
}

function parseJson(text) {
    // Markdownコードブロック除去
    const clean = text.replace(/```json/g, '').replace(/```/g, '').trim();
    try {
        return JSON.parse(clean);
    } catch (e) {
        console.error('JSON Parse Error. Raw text snippet:', clean.slice(0, 200));
        // エラー時はnullではなく空配列を返すか、エラーを投げる
        return [];
    }
}

function toCsv(questions, answers, year, subjectCode) {
    // マージ
    const merged = questions.map(q => {
        const ans = answers.find(a => a.question_no === q.question_no);
        return {
            year,
            subject: subjectCode === 'gyousei' ? '行政法規' : '鑑定評価', // 仮
            question_no: q.question_no,
            statement: q.statement,
            choice1: q.choice1,
            choice2: q.choice2,
            choice3: q.choice3,
            choice4: q.choice4,
            choice5: q.choice5,
            answer: ans ? ans.answer : ''
        };
    }).sort((a, b) => a.question_no - b.question_no);

    // CSVヘッダ
    const header = [
        'year', 'subject', 'question_no', 'statement',
        'choice1', 'choice2', 'choice3', 'choice4', 'choice5', 'answer'
    ];

    // CSV body
    const rows = merged.map(r => {
        return header.map(h => {
            let val = r[h] || '';
            // CSVエスケープ: "を含む場合は""にし、全体を"で囲む
            val = String(val).replace(/"/g, '""');
            if (val.includes(',') || val.includes('\n') || val.includes('"')) {
                val = `"${val}"`;
            }
            return val;
        }).join(',');
    });

    return [header.join(','), ...rows].join('\n');
}

// -------------------------------------------------------------
// Main
// -------------------------------------------------------------

async function main() {
    // 対象年度の決定
    let targetYear = yearArg;
    if (!targetYear) {
        const dirs = fs.readdirSync(RAW_DATA_DIR).filter(d => /^\d{4}$/.test(d));
        if (dirs.length === 0) throw new Error('No data in raw_data/');
        targetYear = Math.max(...dirs.map(Number));
    }
    console.log(`Processing Year: ${targetYear}`);

    const yearDir = path.join(RAW_DATA_DIR, String(targetYear));
    if (!fs.existsSync(yearDir)) throw new Error(`Directory not found: ${yearDir}`);

    // 処理対象: gyousei, kantei
    const subjects = ['gyousei', 'kantei'];

    for (const sub of subjects) {
        const qPdf = path.join(yearDir, `${sub}_question.pdf`);
        const aPdf = path.join(yearDir, `${sub}_answer.pdf`);

        if (!fs.existsSync(qPdf) || !fs.existsSync(aPdf)) {
            console.log(`Skipping ${sub} (PDF missing)`);
            continue;
        }

        // Output path
        // rYY format needed? e.g. r07_gyousei.csv
        // The user system uses rYY. Need to convert 2025 -> r07.
        const eraYear = Number(targetYear) - 2018; // 令和
        const rYY = `r${String(eraYear).padStart(2, '0')}`;
        const outName = `${rYY}_${sub === 'gyousei' ? 'gyousei' : 'kanteihyoka'}.csv`; // Note: kantei vs kanteihyoka in build.js
        const outPath = path.join(DATA_DIR, outName);

        console.log(`\n=== Processing ${sub} -> ${outName} ===`);

        // Upload
        const qFile = await uploadFile(qPdf, 'application/pdf');
        await waitForFileActive(qFile);

        const aFile = await uploadFile(aPdf, 'application/pdf');
        await waitForFileActive(aFile);

        // Extract
        const questions = await extractQuestions(qFile.uri, sub === 'gyousei' ? '行政法規' : '鑑定理論');
        console.log(`Extracted ${questions.length} questions`);

        const answers = await extractAnswers(aFile.uri);
        console.log(`Extracted ${answers.length} answers`);

        // CSV化
        const csv = toCsv(questions, answers, targetYear, sub);

        // 保存
        fs.mkdirSync(path.dirname(outPath), { recursive: true });
        fs.writeFileSync(outPath, csv, 'utf8');
        console.log(`Saved to ${outPath}`);
    }
}

main().catch(e => {
    console.error(e);
    process.exit(1);
});
