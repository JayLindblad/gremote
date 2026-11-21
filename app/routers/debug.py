"""Debug log endpoints (recent commands, results and errors)."""

from fastapi import APIRouter, Depends, Query

from .. import debuglog
from ..auth import require_auth

router = APIRouter(
    prefix="/api/debug",
    tags=["debug"],
    dependencies=[Depends(require_auth)],
)


@router.get("/log")
async def get_log(after: int = Query(default=0, ge=0)):
    return {"entries": debuglog.entries(after)}


@router.delete("/log")
async def clear_log():
    debuglog.clear()
    return {"ok": True}
