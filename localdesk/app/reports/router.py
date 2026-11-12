"""Report endpoints — handover briefing + incident clustering."""

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.reports.clustering import cluster_incidents
from app.reports.handover import generate_handover

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/reports/handover")
async def handover():
    try:
        return await asyncio.to_thread(generate_handover)
    except Exception as e:
        logger.warning("Handover generation failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


@router.post("/api/reports/clusters")
async def clusters():
    try:
        return await asyncio.to_thread(cluster_incidents)
    except Exception as e:
        logger.warning("Clustering failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)
