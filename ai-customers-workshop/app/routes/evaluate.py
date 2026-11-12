"""POST /api/evaluate — agent comparison evaluation."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import PROMPTS_DIR, EXAMPLES_DIR, DEFAULT_MODEL
from app.llm_client import call_llm
from app.routes.examples import PAIRS

logger = logging.getLogger(__name__)
router = APIRouter()


class EvaluateRequest(BaseModel):
    agent: str
    pair_id: str
    model: str = DEFAULT_MODEL
    system_prompt_override: Optional[str] = None


EVALUATION_INSTRUCTION = """Evaluate this website's HTML from your agent perspective. Analyze what you can and cannot extract.

The HTML content of the website "{label}" is below:

<website_html>
{html}
</website_html>

Respond with a JSON object containing these exact fields:
- "verdict": one of "AGENT-FRIENDLY", "AGENT-HOSTILE", or "MIXED"
- "score": integer 0-100 representing overall agent-readiness
- "extracted": object with key facts you were able to find (e.g. product name, price, features, etc.)
- "missing": array of strings — important information you expected but could NOT find or extract
- "findings": array of objects, each with "category" (string), "score" (integer 1-10), "detail" (string explaining what you found or didn't). Categories: "Semantic HTML", "Structured Data", "Content Without JS", "Machine-Readable Data", "Navigation & Links", "Metadata Quality"
- "reasoning": your full analysis as a narrative paragraph explaining your evaluation

Respond ONLY with valid JSON, no other text."""


COMPARISON_INSTRUCTION = """You are summarizing a website evaluation for a workshop audience of engineers and product managers.

Two websites were evaluated by an AI agent persona called "{agent_name}". Here are the results:

**Site A: {label_a}**
Score: {score_a}/100 — {verdict_a}

**Site B: {label_b}**
Score: {score_b}/100 — {verdict_b}

Agent A's reasoning: {reasoning_a}

Agent B's reasoning: {reasoning_b}

Write a compelling 2-4 sentence summary that highlights the key differences. Focus on the "aha moment" — what made one site dramatically better or worse for an AI agent. Be specific about what data was extractable vs. hidden. This is for a live workshop, so make it punchy and educational.

Respond with a JSON object: {{"summary": "your summary text"}}"""


@router.post("/evaluate")
async def evaluate(req: EvaluateRequest):
    # Load system prompt
    if req.system_prompt_override:
        system_prompt = req.system_prompt_override
    else:
        prompt_path = PROMPTS_DIR / f"{req.agent}.md"
        if not prompt_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent prompt not found: {req.agent}")
        system_prompt = prompt_path.read_text()

    # Find the pair
    pair = next((p for p in PAIRS if p["id"] == req.pair_id), None)
    if not pair:
        raise HTTPException(status_code=404, detail=f"Pair not found: {req.pair_id}")

    # Load HTML files
    html_a_path = EXAMPLES_DIR / pair["site_a"]["file"]
    html_b_path = EXAMPLES_DIR / pair["site_b"]["file"]
    if not html_a_path.exists() or not html_b_path.exists():
        raise HTTPException(status_code=404, detail="Example HTML files not found")

    html_a = html_a_path.read_text()
    html_b = html_b_path.read_text()

    # Build messages for parallel evaluation
    messages_a = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": EVALUATION_INSTRUCTION.format(
            label=pair["site_a"]["label"], html=html_a
        )},
    ]
    messages_b = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": EVALUATION_INSTRUCTION.format(
            label=pair["site_b"]["label"], html=html_b
        )},
    ]

    # Run both evaluations in parallel
    try:
        result_a, result_b = await asyncio.gather(
            call_llm(messages_a, model=req.model, json_mode=True),
            call_llm(messages_b, model=req.model, json_mode=True),
        )
    except Exception as e:
        logger.error("LLM evaluation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {str(e)}")

    parsed_a = result_a.get("parsed", {})
    parsed_b = result_b.get("parsed", {})

    # Generate comparison summary
    try:
        summary_messages = [
            {"role": "user", "content": COMPARISON_INSTRUCTION.format(
                agent_name=req.agent.replace("_", " ").title(),
                label_a=pair["site_a"]["label"],
                label_b=pair["site_b"]["label"],
                score_a=parsed_a.get("score", "?"),
                verdict_a=parsed_a.get("verdict", "?"),
                score_b=parsed_b.get("score", "?"),
                verdict_b=parsed_b.get("verdict", "?"),
                reasoning_a=parsed_a.get("reasoning", "N/A"),
                reasoning_b=parsed_b.get("reasoning", "N/A"),
            )},
        ]
        summary_result = await call_llm(summary_messages, model=req.model, json_mode=True, max_tokens=500)
        summary = summary_result.get("parsed", {}).get("summary", "")
    except Exception as e:
        logger.warning("Summary generation failed: %s", e)
        summary = ""

    return {
        "agent": req.agent,
        "model": req.model,
        "pair": pair,
        "site_a": parsed_a,
        "site_b": parsed_b,
        "comparison_summary": summary,
        "usage": {
            "site_a": result_a.get("usage", {}),
            "site_b": result_b.get("usage", {}),
        },
    }
