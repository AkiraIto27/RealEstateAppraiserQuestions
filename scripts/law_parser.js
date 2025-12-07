/**
 * 法律XMLパーサー
 * e-Gov法令APIのXMLフォーマットを解析し、条文テキストを抽出する
 */

import fs from 'node:fs';
import path from 'node:path';
import { XMLParser } from 'fast-xml-parser';

/**
 * トピック名から法律名へのマッピング
 * CSVのtopicフィールドと法律XMLファイル名のマッチングに使用
 */
const TOPIC_TO_LAW_MAP = {
    '土地基本法': '土地基本法',
    '不動産の鑑定評価に関する法律': '不動産の鑑定評価に関する法律',
    '地価公示法': '地価公示法',
    '国土利用計画法': '国土利用計画法',
    '都市計画法': '都市計画法',
    '土地区画整理法': '土地区画整理法',
    '都市再開発法': '都市再開発法',
    '都市緑地法': '都市緑地法',
    '景観法': '景観法',
    '建築基準法': '建築基準法',
    'マンションの建替え等の円滑化に関する法律': 'マンションの建替え等の円滑化に関する法律',
    '不動産登記法': '不動産登記法',
    '住宅の品質確保の促進等に関する法律': '住宅の品質確保の促進等に関する法律',
    '宅地造成等規制法': '宅地造成及び特定盛土等規制法',
    '宅地建物取引業法': '宅地建物取引業法',
    '不動産特定共同事業法': '不動産特定共同事業法',
    '高齢者、障害者等の移動等の円滑化の促進に関する法律': '高齢者、障害者等の移動等の円滑化の促進に関する法律',
    '土地収用法': '土地収用法',
    '土壌汚染対策法': '土壌汚染対策法',
    '文化財保護法': '文化財保護法',
    '自然公園法': '自然公園法',
    '自然環境保全法': '自然環境保全法',
    '農地法': '農地法',
    '道路法': '道路法',
    '河川法': '河川法',
    '国有財産法': '国有財産法',
    '所得税法': '所得税法',
    '法人税法': '法人税法',
    '租税特別措置法': '租税特別措置法',
    '固定資産税': '地方税法',
    '相続税及び贈与税': '相続税法',
    '投資信託及び投資法人に関する法律及び資産の流動化に関する法律': '投資信託及び投資法人に関する法律',
    '金融商品取引法': '金融商品取引法',
};

/**
 * XMLパーサーの設定
 */
const xmlParser = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: '@_',
    textNodeName: '#text',
    trimValues: true,
});

/**
 * 法律XMLファイルを解析し、条文テキストを抽出する
 * @param {string} xmlPath - XMLファイルのパス
 * @returns {object} - 解析結果 { lawName, articles: [{ num, title, content }] }
 */
export function parseLawXml(xmlPath) {
    const xmlContent = fs.readFileSync(xmlPath, 'utf8');
    const parsed = xmlParser.parse(xmlContent);

    const law = parsed.Law;
    if (!law) {
        return { lawName: '', articles: [] };
    }

    const lawBody = law.LawBody;
    if (!lawBody) {
        return { lawName: '', articles: [] };
    }

    const lawName = extractText(lawBody.LawTitle) || '';
    const articles = [];

    // MainProvision から条文を抽出
    const mainProvision = lawBody.MainProvision;
    if (mainProvision) {
        extractArticles(mainProvision, articles);
    }

    return { lawName, articles };
}

/**
 * 再帰的に条文（Article）を抽出する
 */
function extractArticles(node, articles) {
    if (!node) return;

    // 配列の場合は各要素を処理
    if (Array.isArray(node)) {
        node.forEach(item => extractArticles(item, articles));
        return;
    }

    // オブジェクトの場合
    if (typeof node === 'object') {
        // Articleノードの処理
        if (node.Article) {
            const articleNodes = Array.isArray(node.Article) ? node.Article : [node.Article];
            articleNodes.forEach(article => {
                const articleData = parseArticle(article);
                if (articleData) {
                    articles.push(articleData);
                }
            });
        }

        // Chapter, Part などを再帰処理
        for (const key of Object.keys(node)) {
            if (['Chapter', 'Part', 'Section', 'Subsection', 'Division'].includes(key)) {
                extractArticles(node[key], articles);
            }
        }
    }
}

/**
 * 単一のArticleノードを解析する
 */
function parseArticle(article) {
    if (!article) return null;

    const num = article['@_Num'] || '';
    const title = extractText(article.ArticleTitle) || `第${num}条`;
    const caption = extractText(article.ArticleCaption) || '';

    // Paragraphからテキストを抽出
    const paragraphs = [];
    if (article.Paragraph) {
        const paragraphNodes = Array.isArray(article.Paragraph) ? article.Paragraph : [article.Paragraph];
        paragraphNodes.forEach(para => {
            const paraText = extractParagraphText(para);
            if (paraText) {
                paragraphs.push(paraText);
            }
        });
    }

    const content = paragraphs.join('\n');

    return {
        num,
        title,
        caption,
        content,
    };
}

/**
 * Paragraphからテキストを抽出する
 */
function extractParagraphText(paragraph) {
    if (!paragraph) return '';

    const parts = [];

    // ParagraphNumを取得
    const paragraphNum = extractText(paragraph.ParagraphNum);
    if (paragraphNum) {
        parts.push(paragraphNum);
    }

    // ParagraphSentenceを取得
    if (paragraph.ParagraphSentence) {
        const sentenceText = extractSentenceText(paragraph.ParagraphSentence);
        if (sentenceText) {
            parts.push(sentenceText);
        }
    }

    // Itemsを取得
    if (paragraph.Item) {
        const items = Array.isArray(paragraph.Item) ? paragraph.Item : [paragraph.Item];
        items.forEach(item => {
            const itemText = extractItemText(item);
            if (itemText) {
                parts.push(itemText);
            }
        });
    }

    return parts.join(' ');
}

/**
 * Sentenceからテキストを抽出
 */
function extractSentenceText(sentenceNode) {
    if (!sentenceNode) return '';

    if (sentenceNode.Sentence) {
        const sentences = Array.isArray(sentenceNode.Sentence) ? sentenceNode.Sentence : [sentenceNode.Sentence];
        return sentences.map(s => extractText(s)).filter(Boolean).join('');
    }

    return extractText(sentenceNode);
}

/**
 * Itemからテキストを抽出
 */
function extractItemText(item) {
    if (!item) return '';

    const parts = [];

    const itemTitle = extractText(item.ItemTitle);
    if (itemTitle) {
        parts.push(itemTitle);
    }

    if (item.ItemSentence) {
        const sentenceText = extractSentenceText(item.ItemSentence);
        if (sentenceText) {
            parts.push(sentenceText);
        }
    }

    return parts.join(' ');
}

/**
 * ノードからテキストを抽出するヘルパー
 */
function extractText(node) {
    if (!node) return '';
    if (typeof node === 'string') return node;
    if (typeof node === 'number') return String(node);
    if (node['#text']) return node['#text'];
    if (Array.isArray(node)) {
        return node.map(extractText).filter(Boolean).join('');
    }
    if (typeof node === 'object') {
        // 子要素からテキストを抽出
        return Object.values(node)
            .map(extractText)
            .filter(Boolean)
            .join('');
    }
    return '';
}

/**
 * 最新の法律データディレクトリを取得する
 * @param {string} lawsBaseDir - lawsディレクトリのパス
 * @returns {string|null} - 最新の日付フォルダのパス
 */
export function getLatestLawsDir(lawsBaseDir) {
    if (!fs.existsSync(lawsBaseDir)) {
        return null;
    }

    const entries = fs.readdirSync(lawsBaseDir, { withFileTypes: true });
    const dateDirs = entries
        .filter(e => e.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(e.name))
        .map(e => e.name)
        .sort()
        .reverse();

    if (dateDirs.length === 0) {
        return null;
    }

    return path.join(lawsBaseDir, dateDirs[0]);
}

/**
 * トピックに関連する法律XMLファイルを検索する
 * @param {string} topic - 問題のトピック
 * @param {string} lawsDir - 法律データディレクトリ
 * @returns {string[]} - マッチするXMLファイルのパス配列
 */
export function findLawFilesForTopic(topic, lawsDir) {
    if (!topic || !lawsDir) return [];

    // トピックから法律名を取得
    let lawName = TOPIC_TO_LAW_MAP[topic];

    // マッピングにない場合はトピック名をそのまま使用
    if (!lawName) {
        lawName = topic;
    }

    // ディレクトリ内のXMLファイルを検索
    const files = fs.readdirSync(lawsDir);
    const matchingFiles = files.filter(f => {
        if (!f.endsWith('.xml')) return false;
        // ファイル名の先頭が法律名で始まるかチェック
        return f.startsWith(lawName);
    });

    // 最新のバージョンのファイルを優先（日付でソート）
    matchingFiles.sort().reverse();

    return matchingFiles.slice(0, 1).map(f => path.join(lawsDir, f));
}

/**
 * 法律XMLから条文テキストを抽出してRAGコンテキストを構築する
 * @param {string} topic - 問題のトピック
 * @param {string} lawsDir - 法律データディレクトリ
 * @param {number} maxChars - 最大文字数（デフォルト: 30000）
 * @returns {string} - RAGコンテキストテキスト
 */
export function buildRagContext(topic, lawsDir, maxChars = 30000) {
    const lawFiles = findLawFilesForTopic(topic, lawsDir);

    if (lawFiles.length === 0) {
        console.warn(`[law_parser] No law files found for topic: ${topic}`);
        return '';
    }

    const contextParts = [];
    let totalChars = 0;

    for (const lawFile of lawFiles) {
        try {
            const { lawName, articles } = parseLawXml(lawFile);

            if (!lawName || articles.length === 0) {
                continue;
            }

            contextParts.push(`【${lawName}】\n`);
            totalChars += lawName.length + 4;

            for (const article of articles) {
                const articleText = `${article.title}${article.caption ? `（${article.caption}）` : ''}\n${article.content}\n\n`;

                if (totalChars + articleText.length > maxChars) {
                    break;
                }

                contextParts.push(articleText);
                totalChars += articleText.length;
            }
        } catch (error) {
            console.error(`[law_parser] Error parsing ${lawFile}:`, error.message);
        }
    }

    return contextParts.join('');
}

export { TOPIC_TO_LAW_MAP };
