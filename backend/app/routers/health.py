"""Health check endpoint."""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    """Simple health check for Docker/deployment readiness probes."""
    return {"status": "ok"}
