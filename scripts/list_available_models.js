// List available Gemini models directly via API
// Usage: node --env-file=.env scripts/list_available_models.js

const API_KEY = process.env.GEMINI_API_KEY;
if (!API_KEY) {
    console.error("Error: GEMINI_API_KEY is not set.");
    process.exit(1);
}

const URL = `https://generativelanguage.googleapis.com/v1beta/models?key=${API_KEY}`;

async function main() {
    console.log(`Fetching models from ${URL.replace(API_KEY, '***')}...`);
    try {
        const res = await fetch(URL);
        if (!res.ok) {
            console.error(`Error: ${res.status} ${res.statusText}`);
            const text = await res.text();
            console.error(text);
            return;
        }
        const data = await res.json();
        if (data.models) {
            console.log("Available Models:");
            data.models.forEach(m => {
                console.log(`- ${m.name} (${m.displayName}) [methods: ${m.supportedGenerationMethods?.join(', ')}]`);
            });
        } else {
            console.log("No models found in response:", data);
        }
    } catch (e) {
        console.error("Fetch failed:", e);
    }
}

main();
