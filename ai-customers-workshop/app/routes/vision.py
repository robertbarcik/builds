"""POST /api/vision — HTML processing for agent-vision viewer."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.processing.html_processor import process_html

router = APIRouter()


class VisionRequest(BaseModel):
    html: str


@router.post("/vision")
async def vision(req: VisionRequest):
    result = process_html(req.html)
    return result
