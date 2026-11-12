# Builds

Runnable demos and prototypes by Robert Barcik ([barcik.training](https://barcik.training)).
Each folder is one self-contained build: its own README, its own dependencies, its own way to
start. Pick a folder, follow its README, and nothing else in this repo is needed.

| Build | What it shows | Start | Needs |
|---|---|---|---|
| [`localdesk/`](localdesk/) | An IT service desk with an LLM inside: RAG over a knowledge base, function calling, three layers of guardrails, a human approval gate and voice | `./setup.sh`, then `./run.sh` (port 7860) | Python 3.10+, Ollama, an OpenAI or OpenRouter key |
| [`signal-desk/`](signal-desk/) | Monitoring alerts handled twice by an LLM: live triage of an alert stream into a few incident cards, and an agent that studies a month of history and proposes alert rules, each checked by a plain rule engine | `./setup.sh`, then `./run.sh` (port 7870) | Python 3.12, an OpenRouter key |
| [`ai-customers-workshop/`](ai-customers-workshop/) | How AI agents shop: three agent personas judge pairs of example websites and explain what they could and could not read | `python run.py` (port 8000) | Python 3.8+, an OpenRouter key |
| [`pixel-film/`](pixel-film/) | A 21-second pixel-art film drawn and scored entirely in Python code | `python make_film.py film <frames_dir> <out_dir>` | Python with Pillow and numpy, ffmpeg |

## How to use a build

1. Clone this repo and go into the folder of the build you want.
2. Read that folder's README. It lists the setup steps and the keys it expects.
3. Keys go into a `.env` file inside that folder (copy `.env.example`). Never commit it.

Builds that call a language model cost a small amount per session on your own key.

## Elsewhere

- **Browser demos with no setup:** [demos.barcik.training](https://demos.barcik.training)
  (single-page simulations, nothing sent to a model).
- **Publications:** [publications.barcik.training](https://publications.barcik.training)

## AI transparency

These builds were written with substantial help from AI coding agents (Claude) and reviewed
by Robert Barcik. Company names and data inside the demos are fictional; real vendor names
appear for illustration only and imply no affiliation.
