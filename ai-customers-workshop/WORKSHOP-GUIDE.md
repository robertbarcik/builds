# Workshop Facilitator Guide

**Session:** Is Your Website Ready for AI Customers?
**Duration:** 90 minutes (20 min slides + 50 min live demo + 20 min hands-on)
**Audience:** GenAI engineers, product managers, technical-adjacent roles

## Prerequisites

- App running locally (`python run.py`)
- Browser open at `http://localhost:8000`
- OpenRouter API key loaded and working
- Projector/screen share active

## Recommended Demo Flow (50 minutes)

### Part 1: The "Aha Moment" (15 min)

**Goal:** Show that AI agents see the web completely differently than humans.

1. **Start with Agent Vision tab**
   - Load `CloudSync Pro (Marketing Site)` — the bad version
   - Show the side-by-side: "Look how beautiful this site is on the left. Now look at what an AI agent actually sees on the right."
   - Key points to highlight:
     - 0 headings found
     - No structured data
     - JS-dependent elements detected
     - Very little visible text

2. **Now load `CloudSync Pro (Technical Site)`** — the good version
   - Same side-by-side comparison
   - Contrast: 16 headings, JSON-LD structured data, 10/10 semantic score
   - "Same company, same product — but one is invisible to AI agents"

   **Discussion pause:** "Which site would your marketing team build? Which site would an AI procurement agent prefer?"

### Part 2: Agent Evaluation (15 min)

**Goal:** Watch an AI agent evaluate both sites and make a choice.

3. **Switch to Agent Comparison tab**
   - Select VendorScout + CloudSync Pro pair
   - Use a cheap, fast model (DeepSeek V3)
   - Click "Run Evaluation" — takes ~5-10 seconds
   - Show the results side-by-side:
     - Score contrast (bad ~20-40, good ~80-95)
     - What the agent extracted from each
     - What was MISSING from the bad site
     - The agent's reasoning

   **Discussion pause:** "The agent chose the less flashy site. What does this mean for how we build vendor pages?"

4. **Show the system prompt**
   - Click "View / Edit System Prompt"
   - Walk through VendorScout's priorities
   - "This is what's driving the evaluation. The prompt determines what the agent values."

5. **Optional: Edit the prompt live**
   - Add a new priority: "You strongly value visual design and brand aesthetics"
   - Re-run evaluation — show how results change
   - "This demonstrates that agent behavior is prompt-dependent — different AI systems have different priorities"

### Part 3: Framework Documentation (10 min)

**Goal:** Show documentation bias.

6. **Switch agent to StackPicker, pair to DataForge vs QuickQuery**
   - Run evaluation
   - DataForge wins dramatically because it has extractable API docs, benchmarks, getting-started guides
   - QuickQuery looks cooler but an agent can't evaluate "join our Discord"

   **Discussion pause:** "If your framework has great Discord support but sparse docs, an AI developer agent will never recommend it. Is that fair? Does it matter?"

### Part 4: Shopping Comparison (5 min)

7. **Switch agent to DealFinder, pair to ErgoDesk Max**
   - Run evaluation
   - Show how the agent ignores lifestyle imagery and emotional testimonials
   - Focuses purely on extractable specs, price, reviews

### Part 5: Model Disagreement (5 min)

**Goal:** Show that different AI models make different choices.

8. **Switch to Model Comparison tab**
   - Select VendorScout + CloudSync Pro
   - Check 2-3 different models
   - Run all in parallel
   - Show how scores and sometimes verdicts differ
   - "GPT-4o's top pick might not be Claude's top pick — and these models update frequently"

## Hands-On Session (20 minutes)

### Setup (2 min)
- Share the repo URL
- Participants clone, add their own API key, run `python run.py`

### Guided Exploration (8 min)
1. Run all three agent personas on the CloudSync pair
2. Try the Agent Vision tab on each example
3. Compare 2 models on the same evaluation

### Free Exploration (10 min)
Suggest these activities:
- **Try the Audit Tool:** Paste HTML from your own company's website (`View Source` in browser, copy all, paste into the Audit tab)
- **Edit a system prompt:** Make VendorScout care about environmental sustainability, re-run
- **Compare models:** Do Claude and GPT-4o agree on which framework has better docs?

## Key Discussion Questions

Keep these in your back pocket for pause moments:

1. "Your marketing team just spent $200K redesigning the website. An AI agent can't see any of it. Who do you tell first?"
2. "If 30% of your qualified leads in 2027 come through AI agents, what changes in your web strategy?"
3. "Is 'documentation bias' a bug or a feature? Should well-documented products get more AI traffic?"
4. "How do you test if your website is AI-ready? Who owns that in your organization?"
5. "The same site got different scores from different models. How do you optimize for an unstable target?"

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `python run.py` fails | Check Python 3.8+ installed, venv activated, `pip install -r requirements.txt` |
| "LLM call failed" | Check `.env` has valid `OPENROUTER_API_KEY`, check OpenRouter has credit |
| Evaluation takes >30s | Switch to a faster model (DeepSeek V3, Gemini Flash) |
| Browser doesn't open | Navigate manually to `http://localhost:8000` |
| Empty evaluation results | The model may not support JSON mode; try a different model |
