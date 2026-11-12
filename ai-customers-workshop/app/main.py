"""AI Customers Workshop — FastAPI application."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.routes.evaluate import router as evaluate_router
from app.routes.vision import router as vision_router
from app.routes.audit import router as audit_router
from app.routes.prompts import router as prompts_router
from app.routes.examples import router as examples_router
from app.routes.models import router as models_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Customers Workshop")

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include API routers
app.include_router(evaluate_router, prefix="/api")
app.include_router(vision_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
app.include_router(examples_router, prefix="/api")
app.include_router(models_router, prefix="/api")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = Path(__file__).parent.parent / "static" / "index.html"
    return HTMLResponse(index_path.read_text())
