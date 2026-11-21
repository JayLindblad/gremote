"""Device registry CRUD plus status/action endpoints."""

import asyncio
import re
import time
import traceback

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, status
from pydantic import BaseModel

from .. import debuglog
from ..auth import require_auth
from ..db import delete_device, get_device, list_devices, upsert_device
from ..devices import DeviceError, driver_for, known_types

router = APIRouter(
    prefix="/api/devices",
    tags=["devices"],
    dependencies=[Depends(require_auth)],
)

# Last status problem per device, so a TV that stays unreachable is logged once
# instead of on every 20-second poll.
_last_status_problem: dict[str, str | None] = {}


class DeviceIn(BaseModel):
    name: str
    type: str
    host: str
    mac: str | None = None
    config: dict = {}


class ActionRequest(BaseModel):
    action: str
    params: dict = {}


SECRET_CONFIG_KEYS = {"psk"}


def _public(device: dict) -> dict:
    """Device for API responses: secrets stay on the server, the UI only learns they exist."""
    cfg = device.get("config") or {}
    out = dict(device)
    out["config"] = {k: v for k, v in cfg.items() if k not in SECRET_CONFIG_KEYS}
    out["has_psk"] = bool(cfg.get("psk"))
    return out


def _require_device(device_id: str) -> dict:
    device = get_device(device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


def _cause(exc: BaseException) -> str | None:
    cause = exc.__cause__
    if not cause:
        return None
    parts = []
    while cause is not None and len(parts) < 5:
        parts.append(f"{type(cause).__name__}: {cause}".rstrip(": "))
        cause = cause.__cause__ or cause.__context__
    return " <- ".join(parts)


async def _run(device: dict, action: str, params: dict | None, call):
    """Await call(), logging outcome and timing, mapping errors to HTTP codes."""
    name = device["name"]
    started = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = await call()
    except ValueError as exc:
        debuglog.record(str(exc), level="error", kind="command", device=name, action=action,
                        params=params, duration_ms=ms())
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except DeviceError as exc:
        debuglog.record(str(exc), level="error", kind="command", device=name, action=action,
                        params=params, duration_ms=ms(), detail=_cause(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:  # unexpected: keep the traceback for the debug window
        debuglog.record(f"Unexpected {type(exc).__name__}: {exc}", level="error", kind="command",
                        device=name, action=action, params=params, duration_ms=ms(),
                        detail=traceback.format_exc(limit=6))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Unexpected error: {exc}")
    debuglog.record("ok", kind="command", device=name, action=action, params=params,
                    duration_ms=ms(), detail=_short(result))
    return result


def _short(result) -> str | None:
    if result in (None, {}, True):
        return None
    text = str(debuglog.redact(result))
    return text if len(text) <= 300 else text[:300] + "…"


@router.get("/types")
async def types():
    return {"types": known_types()}


@router.get("")
async def index():
    return {"devices": [_public(d) for d in list_devices()]}


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
    debuglog.record(f"Added {device['type']} at {device['host']}", device=device["name"],
                    kind="device")
    if not device.get("mac"):
        device = await _auto_mac(device)
    return {"device": _public(device)}


async def _auto_mac(device: dict) -> dict:
    """Best-effort MAC lookup so Wake-on-LAN works without typing it in."""
    try:
        mac = await driver_for(device).fetch_mac()
    except (DeviceError, ValueError) as exc:
        debuglog.record(f"Could not read MAC automatically: {exc}", level="warn", kind="device",
                        device=device["name"], action="fetch_mac", detail=_cause(exc))
        return device
    if not mac:
        debuglog.record("TV did not report a MAC address; enter it manually", level="warn",
                        kind="device", device=device["name"], action="fetch_mac")
        return device
    debuglog.record(f"MAC detected: {mac}", kind="device", device=device["name"],
                    action="fetch_mac")
    return upsert_device(device["id"], name=device["name"], type=device["type"],
                         host=device["host"], mac=mac, config=device["config"])


@router.post("/{device_id}/fetch-mac")
async def fetch_mac(device_id: str):
    device = _require_device(device_id)
    mac = await _run(device, "fetch_mac", None, lambda: driver_for(device).fetch_mac())
    if not mac:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="The TV did not report a MAC address")
    device = upsert_device(device_id, name=device["name"], type=device["type"],
                           host=device["host"], mac=mac, config=device["config"])
    return {"device": _public(device)}


@router.put("/{device_id}")
async def update(device_id: str, body: DeviceIn):
    existing = _require_device(device_id)
    # Blank config values (e.g. an empty PSK field) mean "keep what is stored".
    config = {**existing["config"], **{k: v for k, v in body.config.items() if v not in (None, "")}}
    mac = body.mac.strip() if body.mac and body.mac.strip() else None
    device = upsert_device(
        device_id,
        name=body.name.strip() or existing["name"],
        type=body.type,
        host=body.host.strip(),
        mac=mac,
        config=config,
    )
    changed = [f for f, old, new in (
        ("name", existing["name"], device["name"]), ("host", existing["host"], device["host"]),
        ("mac", existing.get("mac"), device.get("mac")),
        ("psk", existing["config"].get("psk"), device["config"].get("psk")),
    ) if old != new]
    debuglog.record("Edited (" + (", ".join(changed) or "no changes") + ")", kind="device",
                    device=device["name"])
    _last_status_problem.pop(device_id, None)   # re-evaluate status with the new settings
    return {"device": _public(device)}


@router.delete("/{device_id}")
async def remove(device_id: str):
    device = _require_device(device_id)
    delete_device(device_id)
    debuglog.record("Deleted", kind="device", device=device["name"])
    return {"ok": True}


@router.get("/{device_id}/status")
async def device_status(device_id: str):
    device = _require_device(device_id)
    try:
        result = await driver_for(device).status()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except DeviceError as exc:
        _note_status_problem(device, str(exc), _cause(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    _note_status_problem(device, result.pop("error", None), None)
    return {"status": result}


def _note_status_problem(device: dict, problem: str | None, detail: str | None) -> None:
    previous = _last_status_problem.get(device["id"])
    if problem == previous:
        return
    _last_status_problem[device["id"]] = problem
    if problem:
        debuglog.record(f"Status check failed: {problem}", level="warn", kind="status",
                        device=device["name"], action="status", detail=detail)
    elif previous:
        debuglog.record("Status check recovered", kind="status", device=device["name"],
                        action="status")


@router.get("/{device_id}/screen")
async def device_screen(device_id: str):
    """One PNG frame of the TV's screen over ADB (prototype of a live preview)."""
    device = _require_device(device_id)
    driver = driver_for(device)
    if not hasattr(driver, "screenshot"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="This device type has no screen preview")
    try:
        png = await driver.screenshot()
    except DeviceError as exc:
        _note_status_problem_screen(device, str(exc), _cause(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


_streams: dict[str, asyncio.Task] = {}     # one live stream per device; a new viewer replaces it
START_CODE = b"\x00\x00\x00\x01"
IDLE_FLUSH_SECONDS = 0.06


@router.websocket("/{device_id}/screen/stream")
async def device_screen_stream(ws: WebSocket, device_id: str, size: str = "1280x720",
                               bitrate: int = 4_000_000):
    """Live H.264 video of the TV's screen: binary frames for the browser's MSE player."""
    device = get_device(device_id)
    if not device:
        await ws.close(code=4404)
        return
    driver = driver_for(device)
    if not hasattr(driver, "screen_stream"):
        await ws.close(code=4422)
        return
    if not re.fullmatch(r"\d{3,4}x\d{3,4}", size) or not 250_000 <= bitrate <= 20_000_000:
        await ws.close(code=4422)
        return
    await ws.accept()
    previous = _streams.get(device_id)
    if previous and not previous.done():
        previous.cancel()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def produce():
        try:
            async for chunk in driver.screen_stream(size, bitrate):
                await queue.put(chunk)
            await queue.put(None)
        except Exception as exc:    # reported to the browser below
            await queue.put(exc)

    async def relay():
        buf = b""
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), IDLE_FLUSH_SECONDS)
            except asyncio.TimeoutError:      # stream went quiet: the last frame is complete
                if buf:
                    await ws.send_bytes(buf)
                    buf = b""
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            buf += item
            cut = buf.rfind(START_CODE)       # send only whole NAL units
            if cut > 0:
                await ws.send_bytes(buf[:cut])
                buf = buf[cut:]

    producer = asyncio.create_task(produce())
    relayer = asyncio.create_task(relay())
    listener = asyncio.create_task(ws.receive())    # completes when the browser goes away
    _streams[device_id] = relayer
    debuglog.record("Live screen started", kind="command", device=device["name"], action="stream")
    try:
        await asyncio.wait({relayer, listener}, return_when=asyncio.FIRST_COMPLETED)
        if relayer.done() and not relayer.cancelled() and relayer.exception():
            exc = relayer.exception()
            debuglog.record(f"Live screen failed: {exc}", level="error", kind="command",
                            device=device["name"], action="stream", detail=_cause(exc))
            await ws.send_json({"error": str(exc)})
    except Exception:
        pass
    finally:
        for task in (producer, relayer, listener):
            task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        debuglog.record("Live screen stopped", kind="command", device=device["name"],
                        action="stream")


_last_screen_problem: dict[str, str] = {}


def _note_status_problem_screen(device: dict, problem: str, detail: str | None) -> None:
    """Log a failing preview once, not once per frame."""
    if _last_screen_problem.get(device["id"]) != problem:
        _last_screen_problem[device["id"]] = problem
        debuglog.record(f"Screen preview failed: {problem}", level="error", kind="command",
                        device=device["name"], action="screen", detail=detail)


@router.post("/{device_id}/action")
async def device_action(device_id: str, body: ActionRequest):
    device = _require_device(device_id)
    result = await _run(device, body.action, body.params,
                        lambda: driver_for(device).action(body.action, body.params))
    return {"result": result}
