import { GoogleGenerativeAI } from '@google/generative-ai';

// Load env
try { await import('dotenv/config'); } catch { }

const API_KEY = process.env.GEMINI_API_KEY;
if (!API_KEY) { console.error('No API KEY'); process.exit(1); }

const genAI = new GoogleGenerativeAI(API_KEY);

async function main() {
    try {
        const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        // Can't list models from model instance. Usage:
        // Use ModelManager if available? No, SDK doesn't expose it easily.
        // Actually earlier versions didn't have listModels. v0.21.0 might not have it exposed directly?
        // Wait, current docs say: genAI.getGenerativeModel...
        // Does the API have list models?
        // It's not in the high-level SDK clearly.

        // Try a simple generateContent with a known safe model
        console.log('Testing gemini-2.5-flash...');
        const m = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });
        const r = await m.generateContent("hello");
        console.log('gemini-2.5-flash result:', r.response.text());

    } catch (e) {
        console.error(e);
    }
}

main();
