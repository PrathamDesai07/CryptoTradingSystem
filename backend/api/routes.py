"""HTTP endpoints exposed to the frontend."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/dashboard")
def dashboard() -> dict[str, object]:
    """Placeholder for the combined data consumed by the frontend."""
    return {"ticks": [], "candles": [], "positions": [], "trades": []}
