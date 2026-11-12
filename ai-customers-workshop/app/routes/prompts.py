"""GET/PUT /api/prompts/{agent} — read and edit system prompts."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import PROMPTS_DIR

router = APIRouter()

VALID_AGENTS = ["vendor_scout", "stack_picker", "deal_finder"]


class PromptUpdate(BaseModel):
    prompt: str


@router.get("/prompts/{agent}")
async def get_prompt(agent: str):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    path = PROMPTS_DIR / f"{agent}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Prompt file not found: {agent}.md")
    return {"agent": agent, "prompt": path.read_text()}


@router.put("/prompts/{agent}")
async def update_prompt(agent: str, body: PromptUpdate):
    if agent not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    path = PROMPTS_DIR / f"{agent}.md"
    path.write_text(body.prompt)
    return {"agent": agent, "prompt": body.prompt, "status": "saved"}
