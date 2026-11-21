"""Network discovery endpoints."""

from fastapi import APIRouter, Depends, Query

from ..auth import require_auth
from ..discovery import discover_sony_tvs

router = APIRouter(
    prefix="/api/discovery",
    tags=["discovery"],
    dependencies=[Depends(require_auth)],
)


@router.post("/scan")
async def scan(timeout: float = Query(default=4.0, ge=1.0, le=15.0)):
    devices = await discover_sony_tvs(timeout)
    return {"devices": devices}
