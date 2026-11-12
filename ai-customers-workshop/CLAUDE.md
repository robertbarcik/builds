# AI Customers Workshop — Claude Code Instructions

## What is this?

An interactive workshop tool for "Is Your Website Ready for AI Customers?" — a 90-minute awareness workshop about how AI agents interact with websites differently than humans. Three LLM agent personas evaluate pre-built website pairs and explain their reasoning.

## Tech Stack

- **Backend:** FastAPI + uvicorn, Python 3.8+
- **Frontend:** Single HTML file, Alpine.js (CDN), inline CSS/JS — no build step
- **LLM:** OpenRouter API via OpenAI SDK (`base_url="https://openrouter.ai/api/v1"`)
- **HTML Processing:** BeautifulSoup4 + lxml

## How to Run

```bash
source venv/bin/activate
python run.py
```

Requires `.env` with `OPENROUTER_API_KEY`.

## File Structure

```
app/
  main.py              — FastAPI app, route mounting, index serving
  config.py            — Load .env, expose settings, model list
  llm_client.py        — AsyncOpenAI client for OpenRouter, JSON parsing
  prompts/             — Agent system prompts as .md files (editable via API)
  routes/
    evaluate.py        — POST /api/evaluate (parallel LLM calls for comparison)
    vision.py          — POST /api/vision (HTML processing for agent view)
    audit.py           — POST /api/audit (HTML analysis + LLM scoring)
    prompts.py         — GET/PUT /api/prompts/{agent}
    examples.py        — GET /api/examples, pairs metadata
    models.py          — GET /api/models
  processing/
    html_processor.py  — BeautifulSoup pipeline: extract text, headings, structured data, detect JS dependency
static/
  index.html           — Full frontend: 4 tabs (comparison, vision, model comparison, audit)
examples/
  *.html               — 6 self-contained example websites (3 pairs)
```

## Key Design Decisions

- **OpenRouter over direct API calls:** One key for all models. Swap models in the UI dropdown.
- **Prompts as .md files:** Enables live editing in the UI. Read fresh from disk on every call (no caching).
- **Parallel LLM calls:** `asyncio.gather` for evaluating both sites simultaneously.
- **JSON mode with fallback:** Try `response_format=json_object`, fall back to parsing JSON from markdown blocks.
- **Single-file frontend:** No build step, zero npm dependencies. Alpine.js for reactivity.
- **Python 3.8+ compatible:** Uses `from __future__ import annotations` and `Optional[]` for older Python support.

## Adding a New Agent Persona

1. Create `app/prompts/new_agent.md` with the system prompt
2. Add `"new_agent"` to `VALID_AGENTS` in `app/routes/prompts.py`
3. Add a new pair entry in `app/routes/examples.py` if needed
4. Add the agent option to the `<select>` dropdowns in `static/index.html`

## Adding a New Example Pair

1. Create two HTML files in `examples/`
2. Add a pair entry to the `PAIRS` list in `app/routes/examples.py`

## Common Test

```bash
curl -X POST http://localhost:8000/api/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"agent":"vendor_scout","pair_id":"cloudsync","model":"deepseek/deepseek-chat-v3-0324"}'
```
