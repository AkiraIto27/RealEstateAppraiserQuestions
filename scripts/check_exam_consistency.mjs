#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { parse } from 'csv-parse/sync';

const DEFAULT_DATA_DIR = 'data';
const DEFAULT_BUNDLE_DIRS = ['dist/bundles', 'dist_with_ai/bundles'];
const CSV_PATTERN = /^r\d{2}_(gyousei|kanteihyoka)\.csv$/i;
// Keep these semantics aligned with the consuming app's QuestionExamPattern.
// See RealEstateAppraiser/composeApp/src/commonMain/.../core/model/Question.kt.
const KNOWN_EXAMS = new Set(['combo_iroha', 'single_select', 'fill_blank', 'calc_numeric']);
const REQUIRED_COLUMNS = [
  'id',
  'year',
  'era',
  'era_year',
  'exam',
  'subject',
  'topic',
  'question_no',
  'statement',
  'choice1',
  'choice2',
  'choice3',
  'choice4',
  'choice5',
  'answer',
  'explanation',
  'law_citations',
  'difficulty',
  'tags',
  'source_page',
  'updated_at',
];

const args = parseArgs(process.argv.slice(2));
const dataDir = args.get('--data-dir') ?? DEFAULT_DATA_DIR;
const bundleDirs = args.getAll('--bundle-dir');
const targetBundleDirs = bundleDirs.length > 0 ? bundleDirs : DEFAULT_BUNDLE_DIRS;
const shouldFix = args.has('--fix');
const jsonOutput = args.has('--json');

const allReports = [];

try {
  const csvReports = processCsvFiles(dataDir, shouldFix);
  allReports.push(...csvReports);

  for (const bundleDir of targetBundleDirs) {
    if (!fs.existsSync(bundleDir)) continue;
    allReports.push(...processBundleDir(bundleDir, shouldFix));
  }

  const changed = allReports.filter((report) => report.current !== report.expected);
  const unknown = allReports.filter((report) => report.problem === 'unknown_exam');
  const fixed = shouldFix ? changed.length : 0;

  if (jsonOutput) {
    console.log(JSON.stringify({ checked: allReports.length, changed: changed.length, fixed, reports: changed }, null, 2));
  } else {
    printReports(allReports, shouldFix);
  }

  if (unknown.length > 0) {
    console.error(`[exam] unknown exam values found: ${unknown.length}`);
    process.exit(1);
  }
  if (!shouldFix && changed.length > 0) {
    console.error(`[exam] mismatches found: ${changed.length}`);
    process.exit(1);
  }
  console.log(`[exam] checked=${allReports.length} mismatches=${changed.length} fixed=${fixed}`);
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
}

function processCsvFiles(dir, fix) {
  if (!fs.existsSync(dir)) {
    throw new Error(`data dir not found: ${dir}`);
  }
  const files = fs.readdirSync(dir).filter((file) => CSV_PATTERN.test(file)).sort();
  if (files.length === 0) {
    throw new Error(`no matching CSV files found in ${dir}`);
  }

  const reports = [];
  for (const file of files) {
    const fullPath = path.join(dir, file);
    const csv = fs.readFileSync(fullPath, 'utf8');
    const rows = parse(csv, {
      bom: true,
      columns: true,
      skip_empty_lines: true,
      relax_quotes: true,
    });
    validateColumns(rows, fullPath);

    let changed = false;
    for (const row of rows) {
      const expected = inferExamFromCsvRow(row);
      const current = normalizeExam(row.exam);
      const problem = KNOWN_EXAMS.has(current) ? null : 'unknown_exam';
      reports.push({
        surface: 'csv',
        file: fullPath,
        id: row.id,
        current,
        expected,
        reason: expected.reason,
        problem,
      });

      if (fix && current !== expected.exam) {
        row.exam = expected.exam;
        changed = true;
      }
    }

    if (fix && changed) {
      fs.writeFileSync(fullPath, stringifyCsv(rows, REQUIRED_COLUMNS), 'utf8');
    }
  }
  return reports.map(flattenReport);
}

function processBundleDir(dir, fix) {
  const files = fs.readdirSync(dir).filter((file) => file.endsWith('.jsonl.gz')).sort();
  const reports = [];

  for (const file of files) {
    const fullPath = path.join(dir, file);
    const rows = readJsonlGz(fullPath);
    let changed = false;

    for (const row of rows) {
      const expected = inferExamFromBundleRow(row);
      const current = normalizeExam(row.exam);
      const problem = KNOWN_EXAMS.has(current) ? null : 'unknown_exam';
      reports.push({
        surface: 'bundle',
        file: fullPath,
        id: row.id,
        current,
        expected,
        reason: expected.reason,
        problem,
      });

      if (fix && current !== expected.exam) {
        row.exam = expected.exam;
        changed = true;
      }
    }

    if (fix && changed) {
      writeJsonlGz(fullPath, rows);
    }
  }

  return reports.map(flattenReport);
}

function inferExamFromCsvRow(row) {
  const choices = [1, 2, 3, 4, 5].map((key) => row[`choice${key}`] ?? '');
  return inferExam({
    subject: row.subject ?? '',
    questionNo: row.question_no ?? '',
    statement: row.statement ?? '',
    choices,
  });
}

function inferExamFromBundleRow(row) {
  return inferExam({
    subject: row.subject ?? '',
    questionNo: row.question_no ?? '',
    statement: row.statement ?? '',
    choices: Array.isArray(row.choices) ? row.choices.map((choice) => choice?.text ?? '') : [],
  });
}

function inferExam(question) {
  const statement = normalizeText(question.statement);
  const choices = question.choices.map(normalizeText).filter(Boolean);

  if (isKanteihyokaTableCalculation(question)) {
    return { exam: 'calc_numeric', reason: 'kanteihyoka question 39/40 table calculation' };
  }

  if (hasFillBlankPrompt(statement)) {
    return { exam: 'fill_blank', reason: 'fill-blank prompt' };
  }

  if (isNumericCalculation(statement, choices)) {
    return { exam: 'calc_numeric', reason: 'numeric calculation prompt and numeric choices' };
  }

  if (hasIrohaStatementGroup(statement) && hasIrohaChoiceGroup(choices)) {
    return { exam: 'combo_iroha', reason: 'iroha statement group with iroha-only choice combinations' };
  }

  return { exam: 'single_select', reason: 'standard five-choice sentence selection' };
}

function normalizeExam(value) {
  return String(value ?? '').trim();
}

function normalizeText(value) {
  return String(value ?? '')
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\s+/g, ' ')
    .trim();
}

function hasIrohaStatementGroup(statement) {
  if (/次の[ 　]*[イロハニホ]から[イロハニホ]までの記述/.test(statement)) return true;
  if (/[イロハニホ]から[イロハニホ]までの記述/.test(statement)) return true;

  const rawSegments = String(statement ?? '')
    .replace(/\\n/g, '\n')
    .split('</border>')
    .slice(1)
    .map((segment) => segment.trim());
  return rawSegments.some((segment) => /^[イロハニホ][ 　]/.test(segment));
}

function hasIrohaChoiceGroup(choices) {
  return choices.length === 5 && choices.every(isIrohaCombinationChoice);
}

function isIrohaCombinationChoice(choice) {
  const normalized = choice.replace(/[、,]/g, ' ').replace(/\s+/g, '');
  return /^(?:[イロハニホ](?:と[イロハニホ]){0,4}|[イロハニホ]のみ|該当なし)$/.test(normalized);
}

function hasFillBlankPrompt(statement) {
  return /空欄|穴埋め|[（(][ 　]*[イロハニホ][ 　]*[）)]/.test(statement);
}

function isKanteihyokaTableCalculation(question) {
  return normalizeText(question.subject) === '不動産の鑑定評価に関する理論'
    && [39, 40].includes(Number(question.questionNo))
    && hasMarkdownTable(question.statement ?? '');
}

function hasMarkdownTable(statement) {
  const text = String(statement ?? '').replace(/\\r\\n/g, '\n').replace(/\\n/g, '\n');
  return /(^|\n)\|[^|\n]+\|/.test(text) && /(^|\n)\|[-:| ]+\|/.test(text);
}

function isNumericCalculation(statement, choices) {
  if (choices.length !== 5) return false;
  if (!/(計算|計算結果|前提条件|数値|収益価格|比準価格|積算価格|試算賃料|実質賃料|利回り)/.test(statement)) {
    return false;
  }
  return choices.every(isNumericChoice);
}

function isNumericChoice(choice) {
  if (/^[イロハニホ][ 　:：]/.test(choice)) return false;
  if (!/[0-9０-９]/.test(choice)) return false;
  if (!/[円％%㎡mM]|メートル|m2|㎡/.test(choice)) return false;
  return choice.length <= 40;
}

function validateColumns(rows, file) {
  if (rows.length === 0) {
    throw new Error(`CSV has no rows: ${file}`);
  }
  const columns = Object.keys(rows[0]);
  const missing = REQUIRED_COLUMNS.filter((column) => !columns.includes(column));
  if (missing.length > 0) {
    throw new Error(`${file} missing columns: ${missing.join(', ')}`);
  }
}

function stringifyCsv(rows, columns) {
  const lines = [
    columns.join(','),
    ...rows.map((row) => columns.map((column) => escapeCsvCell(row[column] ?? '')).join(',')),
  ];
  return `\uFEFF${lines.join('\r\n')}\r\n`;
}

function escapeCsvCell(value) {
  const text = String(value ?? '');
  if (/[",\r\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function readJsonlGz(file) {
  const text = zlib.gunzipSync(fs.readFileSync(file)).toString('utf8');
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function writeJsonlGz(file, rows) {
  const jsonl = rows.map((row) => JSON.stringify(row, null, 0)).join('\n') + '\n';
  fs.writeFileSync(file, zlib.gzipSync(jsonl));
}

function flattenReport(report) {
  return {
    surface: report.surface,
    file: report.file,
    id: report.id,
    current: report.current,
    expected: report.expected.exam,
    reason: report.reason,
    problem: report.problem,
  };
}

function printReports(reports, fix) {
  const changed = reports.filter((report) => report.current !== report.expected);
  const unknown = reports.filter((report) => report.problem === 'unknown_exam');
  const action = fix ? 'fixed' : 'mismatch';

  for (const report of changed) {
    console.log(
      `[exam] ${action} ${report.surface} ${report.id} ${report.current || '(blank)'} -> ${report.expected} (${report.reason}) ${report.file}`,
    );
  }
  for (const report of unknown) {
    console.log(`[exam] unknown ${report.surface} ${report.id} exam=${report.current} ${report.file}`);
  }
  if (changed.length === 0 && unknown.length === 0) {
    console.log('[exam] no mismatches');
  }
}

function parseArgs(rawArgs) {
  const values = new Map();
  const flags = new Set();

  for (let index = 0; index < rawArgs.length; index += 1) {
    const arg = rawArgs[index];
    if (!arg.startsWith('--')) continue;
    if (arg === '--fix' || arg === '--check' || arg === '--json') {
      flags.add(arg);
      continue;
    }
    const next = rawArgs[index + 1];
    if (!next || next.startsWith('--')) {
      throw new Error(`missing value for ${arg}`);
    }
    if (!values.has(arg)) values.set(arg, []);
    values.get(arg).push(next);
    index += 1;
  }

  return {
    has(name) {
      return flags.has(name);
    },
    get(name) {
      return values.get(name)?.[0];
    },
    getAll(name) {
      return values.get(name) ?? [];
    },
  };
}
