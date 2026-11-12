"""POST /api/audit — agent-readiness audit tool."""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import PROMPTS_DIR, DEFAULT_MODEL
from app.llm_client import call_llm
from app.processing.html_processor import process_html

logger = logging.getLogger(__name__)
router = APIRouter()


class AuditRequest(BaseModel):
    html: str
    model: str = DEFAULT_MODEL


@router.post("/audit")
async def audit(req: AuditRequest):
    # Step 1: Deterministic HTML analysis
    analysis = process_html(req.html)

    # Step 2: Load audit prompt
    audit_prompt_path = PROMPTS_DIR / "audit.md"
    if not audit_prompt_path.exists():
        raise HTTPException(status_code=500, detail="Audit prompt not found")
    audit_prompt = audit_prompt_path.read_text()

    # Step 3: LLM scoring with context from deterministic analysis
    messages = [
        {"role": "system", "content": audit_prompt},
        {"role": "user", "content": f"""Evaluate this website's AI agent readiness.

## Structural Analysis (automated)
{json.dumps(analysis["stats"], indent=2)}

## Detected Features
- Headings found: {len(analysis["agent_view"]["headings"])}
- Links found: {len(analysis["agent_view"]["links"])}
- Images found: {len(analysis["agent_view"]["images"])}
- Structured data: {"Yes" if analysis["agent_view"]["structured_data"] else "None found"}
- JS-dependent elements: {len(analysis["agent_view"]["js_dependent_elements"])}
- Meta tags: {json.dumps(analysis["agent_view"]["meta"], indent=2)}

## Raw HTML
<website_html>
{req.html}
</website_html>

Score each of the 10 categories (1-10) and provide your analysis as JSON."""},
    ]

    try:
        result = await call_llm(messages, model=req.model, json_mode=True, max_tokens=3000)
    except Exception as e:
        logger.error("Audit LLM call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {str(e)}")

    parsed = result.get("parsed", {})

    return {
        "audit": parsed,
        "structural_analysis": analysis,
        "model": req.model,
        "usage": result.get("usage", {}),
    }
