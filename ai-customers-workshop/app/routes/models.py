"""GET /api/models — available LLM models."""

from fastapi import APIRouter
from app.config import MODELS, DEFAULT_MODEL

router = APIRouter()


@router.get("/models")
async def list_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}
