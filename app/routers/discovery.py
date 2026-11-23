"""Network discovery endpoints."""

import time

from fastapi import APIRouter, Depends, Query

from .. import debuglog
from ..auth import require_auth
from ..discovery import discover_sony_tvs, sweep_subnets

router = APIRouter(
    prefix="/api/discovery",
    tags=["discovery"],
    dependencies=[Depends(require_auth)],
)


@router.post("/scan")
async def scan(timeout: float = Query(default=4.0, ge=1.0, le=15.0)):
    started = time.monotonic()
    try:
        devices = await discover_sony_tvs(timeout)
    except Exception as exc:
        debuglog.record(f"Scan failed: {type(exc).__name__}: {exc}", level="error", kind="scan",
                        action="scan")
        raise
    debuglog.record(f"Found {len(devices)} Sony TV(s)" + (
        ": " + ", ".join(f'{d["host"]} ({"+".join(d.get("found_by", []))})' for d in devices) if devices else
        f" (swept {', '.join(sweep_subnets()) or 'nothing'}; check IP control is on, and set "
        "GREMOTE_SCAN_SUBNET if the TV is on another subnet)"),
        level="info" if devices else "warn", kind="scan", action="scan",
        duration_ms=int((time.monotonic() - started) * 1000))
    return {"devices": devices}
