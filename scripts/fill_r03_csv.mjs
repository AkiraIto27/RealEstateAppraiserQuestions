
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'csv-parse/sync'; // Using sync for simplicity as file is not huge
import { GoogleGenerativeAI } from '@google/generative-ai';
import { buildRagContext } from './law_parser.js';

// Configuration
const CSV_FILE = 'data/r03_gyousei.csv';
const LAWS_DIR = 'laws/2024-09-01'; // Explicitly requested by user
const MODEL_NAME = 'gemini-2.5-flash';

if (!process.env.GEMINI_API_KEY) {
    console.error('Error: GEMINI_API_KEY is not set.');
    process.exit(1);
}

const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
const model = genAI.getGenerativeModel({ model: MODEL_NAME });

// Helper to escape CSV fields
function escapeCsvField(field) {
    if (field === null || field === undefined) return '';
    const stringField = String(field);
    if (stringField.includes('"') || stringField.includes(',') || stringField.includes('\n') || stringField.includes('\r')) {
        return `"${stringField.replace(/"/g, '""')}"`;
    }
    return stringField;
}

// Helper to write CSV
function writeCsv(headers, rows, outputPath) {
    const headerLine = headers.map(escapeCsvField).join(',');
    const body = rows.map(row => {
        return headers.map(header => escapeCsvField(row[header])).join(',');
    }).join('\n');
    fs.writeFileSync(outputPath, headerLine + '\n' + body);
}

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function generateExplanation(question, context) {
    const prompt = `
あなたは不動産鑑定士試験の専門家です。以下の問題について、関連する法令に基づいて解説を作成してください。

## 関連法令
${context}

## 問題
${question.statement}

## 選択肢
1: ${question.choice1}
2: ${question.choice2}
3: ${question.choice3}
4: ${question.choice4}
5: ${question.choice5}

## 正解
${question.answer}

## 出力形式
以下のJSON形式で出力してください。
\`\`\`json
{
  "explanation": "解説文...",
  "law_citations": ["法令名 第X条", ...]
}
\`\`\`

解説文では、正解の理由と、各選択肢の誤りの理由を具体的に説明してください。
`;

    const result = await model.generateContent(prompt);
    const response = await result.response;
    const text = response.text();
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (jsonMatch) {
        return JSON.parse(jsonMatch[0]);
    }
    throw new Error('Failed to parse JSON response');
}

// Concurrency limit helper
async function mapLimit(items, limit, fn) {
    const results = [];
    const executing = [];
    for (const item of items) {
        const p = Promise.resolve().then(() => fn(item));
        results.push(p);
        const e = p.then(() => executing.splice(executing.indexOf(e), 1));
        executing.push(e);
        if (executing.length >= limit) {
            await Promise.race(executing);
        }
    }
    return Promise.all(results);
}

async function generateExplanationWithRetry(question, context, retries = 3) {
    for (let i = 0; i < retries; i++) {
        try {
            const res = await generateExplanation(question, context);
            if (res) return res;
        } catch (e) {
            console.error(`Error processing ${question.id}, attempt ${i + 1}/${retries}:`, e.message);
            if (e.message.includes('429')) {
                console.log(`Rate limited. Waiting 60s...`);
                await sleep(60000);
            } else {
                await sleep(1000);
            }
        }
    }
    return null;
}

async function main() {
    console.log(`Reading ${CSV_FILE}...`);
    const csvContent = fs.readFileSync(CSV_FILE, 'utf8');
    const records = parse(csvContent, {
        columns: true,
        skip_empty_lines: true
    });

    console.log(`Found ${records.length} records. Starting parallel processing (concurrency: 5)...`);

    const headers = Object.keys(records[0]);
    let processedCount = 0;
    let updatedCount = 0;

    await mapLimit(records, 1, async (record) => {
        // Skip if explanation already exists
        if (record.explanation && record.explanation.trim() !== '') {
            return;
        }

        console.log(`Processing ${record.id} (${record.topic})...`);

        const context = buildRagContext(record.topic, LAWS_DIR);
        if (!context) {
            console.warn(`No context found for topic: ${record.topic}`);
            return;
        }

        const result = await generateExplanationWithRetry(record, context, 10); // Increase retries to 10

        if (result) {
            record.explanation = result.explanation;
            if (Array.isArray(result.law_citations)) {
                record.law_citations = result.law_citations.join('; ');
            } else {
                record.law_citations = result.law_citations;
            }
            record.updated_at = new Date().toISOString();
            updatedCount++;
            console.log(`  [Done] ${record.id}`);
            // Save incrementally
            writeCsv(headers, records, CSV_FILE);
        } else {
            console.error(`  [Failed] ${record.id}`);
        }
        processedCount++;
    });


    if (updatedCount > 0) {
        console.log(`Writing updated CSV to ${CSV_FILE}...`);
        writeCsv(headers, records, CSV_FILE);
        console.log(`Done. Updated ${updatedCount} records.`);
    } else {
        console.log('No records updated.');
    }
}

main().catch(console.error);
