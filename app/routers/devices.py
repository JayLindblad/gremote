"""Device registry CRUD plus status/action endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import require_auth
from ..db import delete_device, get_device, list_devices, upsert_device
from ..devices import DeviceError, driver_for, known_types

router = APIRouter(
    prefix="/api/devices",
    tags=["devices"],
    dependencies=[Depends(require_auth)],
)


class DeviceIn(BaseModel):
    name: str
    type: str
    host: str
    mac: str | None = None
    config: dict = {}


class ActionRequest(BaseModel):
    action: str
    params: dict = {}


def _require_device(device_id: str) -> dict:
    device = get_device(device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.get("/types")
async def types():
    return {"types": known_types()}


@router.get("")
async def index():
    return {"devices": list_devices()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: DeviceIn):
    device = upsert_device(
        None,
        name=body.name,
        type=body.type,
        host=body.host,
        mac=body.mac,
        config=body.config,
    )
    return {"device": device}


@router.put("/{device_id}")
async def update(device_id: str, body: DeviceIn):
    _require_device(device_id)
    device = upsert_device(
        device_id,
        name=body.name,
        type=body.type,
        host=body.host,
        mac=body.mac,
        config=body.config,
    )
    return {"device": device}


@router.delete("/{device_id}")
async def remove(device_id: str):
    _require_device(device_id)
    delete_device(device_id)
    return {"ok": True}


@router.get("/{device_id}/status")
async def device_status(device_id: str):
    device = _require_device(device_id)
    try:
        return {"status": await driver_for(device).status()}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except DeviceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/{device_id}/action")
async def device_action(device_id: str, body: ActionRequest):
    device = _require_device(device_id)
    try:
        result = await driver_for(device).action(body.action, body.params)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except DeviceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return {"result": result}
