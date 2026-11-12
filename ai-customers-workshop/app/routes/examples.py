"""GET /api/examples — list and serve example HTML files."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import EXAMPLES_DIR

router = APIRouter()

PAIRS = [
    {
        "id": "cloudsync",
        "name": "CloudSync Pro — SaaS Product Pages",
        "description": "Enterprise SaaS vendor evaluation: JS-heavy marketing vs. semantic HTML with clear pricing",
        "agent": "vendor_scout",
        "site_a": {"file": "cloudsync-bad.html", "label": "CloudSync Pro (Marketing Site)"},
        "site_b": {"file": "cloudsync-good.html", "label": "CloudSync Pro (Technical Site)"},
    },
    {
        "id": "frameworks",
        "name": "DataForge vs QuickQuery — Framework Docs",
        "description": "Developer tool evaluation: comprehensive docs vs. sparse README",
        "agent": "stack_picker",
        "site_a": {"file": "quickquery.html", "label": "QuickQuery"},
        "site_b": {"file": "dataforge.html", "label": "DataForge"},
    },
    {
        "id": "ergodesk",
        "name": "ErgoDesk Max — Product Comparison",
        "description": "Shopping comparison: lifestyle imagery vs. structured product data",
        "agent": "deal_finder",
        "site_a": {"file": "ergodesk-bad.html", "label": "ErgoDesk Max (Lifestyle Site)"},
        "site_b": {"file": "ergodesk-good.html", "label": "ErgoDesk Max (Specs Site)"},
    },
]


@router.get("/examples")
async def list_examples():
    return {"pairs": PAIRS}


@router.get("/examples/{filename}")
async def get_example(filename: str):
    filepath = EXAMPLES_DIR / filename
    if not filepath.exists() or not filepath.suffix == ".html":
        raise HTTPException(status_code=404, detail=f"Example not found: {filename}")
    return PlainTextResponse(filepath.read_text())
