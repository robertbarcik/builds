# AI Customers Workshop

Interactive tools for the **"Is Your Website Ready for AI Customers?"** awareness workshop. This repo powers the live demo and hands-on portions of a 90-minute session.

**What it does:** Simplified LLM agents evaluate pairs of websites and explain why some sites are invisible to AI agents while others are easily readable. The audience watches agents ignore beautiful marketing sites and prefer well-structured ones — then explores why.

> This is an educational tool for awareness workshops, not a production system.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/robertbarcik/ai-customers-workshop.git
cd ai-customers-workshop

# 2. Set up environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Add your OpenRouter API key
cp .env.example .env
# Edit .env and paste your key from https://openrouter.ai/keys

# 4. Launch
python run.py
```

The browser opens automatically at `http://localhost:8000`.

## What's Inside

### Agent Personas

Three AI agent personas, each evaluating websites from a different perspective:

| Agent | Role | Evaluates |
|-------|------|-----------|
| **VendorScout** | Enterprise procurement | SaaS vendor pages — pricing, SLAs, certifications |
| **StackPicker** | Developer evaluation | Framework docs — API reference, examples, getting-started |
| **DealFinder** | Consumer shopping | Product pages — specs, price, reviews, availability |

System prompts are visible and editable in the UI.

### Example Website Pairs

Pre-built HTML files demonstrating agent-friendly vs. agent-hostile design:

1. **CloudSync Pro** — SaaS pages: JS-heavy marketing site vs. semantic HTML with clear pricing
2. **DataForge vs QuickQuery** — Framework docs: comprehensive docs vs. sparse README
3. **ErgoDesk Max** — Product pages: lifestyle imagery vs. structured spec tables

### Four Modes

- **Agent Comparison** — Run an agent on a website pair, see what it extracts vs. can't find
- **Agent Vision** — Side-by-side human view vs. what an AI crawler actually receives
- **Model Comparison** — Same evaluation across 2-3 different LLMs — shows model disagreement
- **Audit Tool** — Paste any HTML, get a 10-category agent-readiness scorecard

## Requirements

- Python 3.8+
- OpenRouter API key (get one at [openrouter.ai/keys](https://openrouter.ai/keys))

## Tech Stack

- **Backend:** FastAPI + uvicorn
- **Frontend:** Vanilla HTML/CSS/JS + Alpine.js (CDN)
- **LLM:** OpenRouter API (any model — DeepSeek, Claude, GPT-4o, Gemini, Llama)
- **HTML Processing:** BeautifulSoup4

No build step. No database. No Docker. Clone and run.

## Configuration

Edit `.env` to change defaults:

```bash
OPENROUTER_API_KEY=sk-or-v1-...    # Required
DEFAULT_MODEL=deepseek/deepseek-chat-v3-0324  # Optional
PORT=8000                            # Optional
```

Models can also be switched in the UI at any time.

## License

Educational use. Built for [barcik.training](https://barcik.training) workshops.
