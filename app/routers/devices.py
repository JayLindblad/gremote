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
from ..devices import DeviceError, driver_for, known_types, xfinity

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
    box = cfg.get("xfinity")
    if box:
        out["config"]["xfinity"] = {"paired": bool(box.get("token")), "mode": box.get("mode", "auto"),
                                    "port": box.get("port")}
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


_page_scans: dict[str, asyncio.Task] = {}   # one Settings-page scan per device at a time


async def _scan_and_save(device_id: str) -> dict:
    """Scan the TV's Settings pages and save the result; joins a scan already running."""
    task = _page_scans.get(device_id)
    if task is None or task.done():
        async def work() -> dict:
            device = _require_device(device_id)
            result = await _run(device, "adb_scan_pages", None,
                                lambda: driver_for(device).action("adb_scan_pages", {}))
            device = _require_device(device_id)        # re-read: may have been edited meanwhile
            config = {**device["config"], "settings_pages": {
                "pages": result.get("pages", []), "targets": result.get("targets", {}),
                "extra": result.get("extra", []), "checked": int(time.time())}}
            return upsert_device(device_id, name=device["name"], type=device["type"],
                                 host=device["host"], mac=device.get("mac"), config=config)
        task = _page_scans[device_id] = asyncio.create_task(work())
        task.add_done_callback(lambda t: _page_scans.pop(device_id, None)
                               if _page_scans.get(device_id) is t else None)
    return await asyncio.shield(task)


@router.post("/{device_id}/scan-pages")
async def scan_pages(device_id: str):
    """Ask the TV (over ADB) which Settings pages it has, and remember the answer."""
    _require_device(device_id)
    return {"device": _public(await _scan_and_save(device_id))}


@router.put("/{device_id}")
async def update(device_id: str, body: DeviceIn):
    existing = _require_device(device_id)
    # Blank config values (e.g. an empty PSK field) mean "keep what is stored".
    config = {**existing["config"], **{k: v for k, v in body.config.items() if v not in (None, "")}}
    if body.host.strip() != existing["host"]:
        config.pop("settings_pages", None)     # another TV: check its pages again
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
    if _box(device) and result.get("power") == "active":
        await _refresh_input(device)
        result["cable_box"] = _box_active_cached(device)
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
BIG_FRAME_BYTES = 8000


def _flush_after(buf: bytes, quiet: float) -> float | None:
    """How long to wait for more data before treating the buffered tail as a whole NAL unit.

    `quiet` is the stream's own pace (short when bytes arrive promptly, long when they come in
    batches). None means wait indefinitely: the buffer holds only a start code, so sending it
    would cut the next frame in two.
    """
    if len(buf) <= len(START_CODE) + 1:
        return None
    if buf.startswith(START_CODE) and buf[4] & 31 == 5:     # IDR keyframe
        return max(quiet, 0.15)
    return max(quiet, 0.1) if len(buf) > BIG_FRAME_BYTES else quiet


@router.websocket("/{device_id}/screen/stream")
async def device_screen_stream(ws: WebSocket, device_id: str, size: str = "640x360",
                               bitrate: int = 1_000_000, diag: int = 0):
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
    send_lock = asyncio.Lock()
    t0 = time.monotonic()
    sent = {"bytes": 0, "first": False}
    quiet = {"seconds": 0.35}      # replaced by the stream's own pace once it reports its mode

    async def note(text: str) -> None:
        """Diagnostic line for the browser's recorded log (only when it asked for one)."""
        if diag:
            async with send_lock:
                await ws.send_json({"diag": f"{time.monotonic() - t0:6.2f}s {text}"})

    async def send(data: bytes) -> None:
        async with send_lock:
            await ws.send_bytes(data)
        if not sent["first"]:
            sent["first"] = True
            await note(f"first video bytes to browser ({len(data)} bytes, starts "
                       f"{data[:12].hex(' ')})")
        sent["bytes"] += len(data)

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
                item = await asyncio.wait_for(queue.get(), _flush_after(buf, quiet["seconds"]))
            except asyncio.TimeoutError:      # stream went quiet: the last frame is complete
                if buf:
                    await send(buf)
                    buf = b""
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            if isinstance(item, tuple):         # ("mode", name, quiet_seconds) from the stream
                quiet["seconds"] = item[2]
                await note(f"capture mode: {item[1]} (frame is complete after {item[2]} s of quiet)")
                continue
            buf += item
            cut = buf.rfind(START_CODE)       # send only whole NAL units
            if cut > 0:
                await send(buf[:cut])
                buf = buf[cut:]

    async def diagnostics():
        """Server-side view while recording: byte counts and what the TV says about screenrecord."""
        await note(f"stream requested: size={size} bitrate={bitrate} device={device['host']}")
        elapsed = 0
        while True:
            await asyncio.sleep(1)
            elapsed += 1
            await note(f"relay: {sent['bytes']} bytes sent so far, queue={queue.qsize()}")
            if elapsed in (4, 12):
                try:
                    report = await driver.screen_diagnostics()
                except Exception as exc:
                    report = f"could not collect: {type(exc).__name__}: {exc}"
                await note(f"TV report at {elapsed}s:\n{report}")

    producer = asyncio.create_task(produce())
    relayer = asyncio.create_task(relay())
    listener = asyncio.create_task(ws.receive())    # completes when the browser goes away
    diag_task = asyncio.create_task(diagnostics()) if diag else None
    _streams[device_id] = relayer
    debuglog.record("Live screen started", kind="command", device=device["name"], action="stream")
    try:
        await asyncio.wait({relayer, listener}, return_when=asyncio.FIRST_COMPLETED)
        if relayer.done() and not relayer.cancelled() and relayer.exception():
            exc = relayer.exception()
            debuglog.record(f"Live screen failed: {exc}", level="error", kind="command",
                            device=device["name"], action="stream", detail=_cause(exc))
            async with send_lock:
                await ws.send_json({"error": str(exc)})
    except Exception:
        pass
    finally:
        for task in (producer, relayer, listener, diag_task):
            if task:
                task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        debuglog.record("Live screen stopped", kind="command", device=device["name"],
                        action="stream")


@router.get("/{device_id}/screen/sample")
async def device_screen_sample(device_id: str, size: str = "1024x576",
                               bitrate: int = 3_000_000, seconds: int = 5):
    """A few seconds of the TV's raw H.264 stream, for checking it offline (VLC, ffprobe)."""
    device = _require_device(device_id)
    driver = driver_for(device)
    if not hasattr(driver, "screen_stream"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="This device type has no screen stream")
    if not re.fullmatch(r"\d{3,4}x\d{3,4}", size) or not 250_000 <= bitrate <= 20_000_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid size or bitrate")
    seconds = max(2, min(seconds, 15))
    chunks: list[bytes] = []

    async def collect():
        async for chunk in driver.screen_stream(size, bitrate):
            if isinstance(chunk, bytes):        # skip the leading ("mode", ...) announcement
                chunks.append(chunk)

    try:
        await asyncio.wait_for(collect(), seconds + 1.5)    # stops on the timer, by design
    except asyncio.TimeoutError:
        pass
    except DeviceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    data = b"".join(chunks)
    debuglog.record(f"Raw sample recorded: {len(data)} bytes at {size}", kind="command",
                    device=device["name"], action="screen_sample")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="tv-sample.h264"'})


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
    page = body.params.get("page") if body.action == "adb_open" else None
    if page and not body.params.get("intent") and device["type"] == "sony_bravia":
        # Open the page found for this shortcut on this TV. Not checked yet (or a check is
        # still running): finish the check first, rather than guessing with a standard action.
        saved = (device["config"].get("settings_pages") or {}).get("targets")
        if saved is None:
            try:
                device = await _scan_and_save(device_id)
                saved = device["config"]["settings_pages"]["targets"]
            except HTTPException:
                saved = {}                                # no ADB scan possible: plain fallback
        if page in saved:
            body.params = {**body.params, "intent": saved[page]}
        elif saved:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This TV has no {page} page")
    if body.action in ("xfinity_key", "xfinity_say"):
        return {"result": await _run(device, body.action, body.params,
                                     lambda: _box_send(device_id, body.action, body.params))}
    if body.action == "ircc" and _box(device):
        vcode = xfinity.vcode_for(str(body.params.get("code", "")))
        if vcode and await _box_active(device):
            result = await _run(device, "xfinity_key", {"vcode": vcode, "via": body.params.get("code")},
                                lambda: _box_send(device_id, "xfinity_key", {"vcode": vcode}))
            return {"result": {**result, "to": "cable_box"}}
    result = await _run(device, body.action, body.params,
                        lambda: driver_for(device).action(body.action, body.params))
    if body.action == "set_input" and _box(device):
        uri = body.params.get("uri") or ""
        if uri:
            _inputs[device_id] = (uri, time.monotonic())
        else:
            _inputs.pop(device_id, None)
    return {"result": result}


# -- Xfinity X1 box attached to a TV (Comcast's Adaptive Remote cloud service) ----------------
# Pairing gives a token that is stored with the TV. When the TV shows the box's HDMI input
# (mode "auto"), or always (mode "box"), navigation/channel/number/playback keys go to the box.

_pairing: dict[str, dict] = {}                     # device id -> pairing in progress
_inputs: dict[str, tuple[str, float] | None] = {}  # device id -> (TV's current input uri, when)
_input_checks: dict[str, asyncio.Task] = {}
INPUT_FRESH = 8.0          # seconds a known input is trusted before it is checked again
_PORT = re.compile(r"(?:^|[?&])port=(\d+)")


class BoxSettings(BaseModel):
    mode: str | None = None    # auto | box | off
    port: int | None = None    # the TV's HDMI port the box is plugged into


def _box(device: dict) -> dict | None:
    box = device["config"].get("xfinity")
    return box if box and box.get("token") and box.get("mode", "auto") != "off" else None


def _save_box(device_id: str, box: dict | None) -> dict:
    device = _require_device(device_id)               # re-read: may have changed meanwhile
    config = dict(device["config"])
    if box is None:
        config.pop("xfinity", None)
    else:
        config["xfinity"] = box
    return upsert_device(device_id, name=device["name"], type=device["type"],
                         host=device["host"], mac=device.get("mac"), config=config)


def _input_port(uri: str) -> int | None:
    if not uri.startswith(("extInput:hdmi", "extInput:cec")):
        return None
    m = _PORT.search(uri.split("?", 1)[1] if "?" in uri else "")
    return int(m.group(1)) if m else None


def _box_active_cached(device: dict) -> bool:
    box = _box(device)
    if not box:
        return False
    if box.get("mode") == "box":
        return True
    seen = _inputs.get(device["id"])
    return bool(seen and box.get("port") and _input_port(seen[0]) == box["port"])


async def _refresh_input(device: dict) -> None:
    """Ask the TV what it is showing (shared between concurrent callers)."""
    task = _input_checks.get(device["id"])
    if task is None or task.done():
        async def work():
            try:
                playing = await driver_for(device).action("playing")
                _inputs[device["id"]] = ((playing or {}).get("uri") or "", time.monotonic())
            except DeviceError:
                _inputs[device["id"]] = ("", time.monotonic())
        task = _input_checks[device["id"]] = asyncio.create_task(work())
    await asyncio.shield(task)


async def _box_active(device: dict) -> bool:
    box = _box(device)
    if not box or box.get("mode") == "box":
        return bool(box)
    seen = _inputs.get(device["id"])
    if seen is None:
        await _refresh_input(device)                   # first key: find out now
    elif time.monotonic() - seen[1] > INPUT_FRESH:
        asyncio.create_task(_refresh_input(device))    # use what we know; check for next time
    return _box_active_cached(device)


async def _box_send(device_id: str, action: str, params: dict) -> dict:
    device = _require_device(device_id)
    box = device["config"].get("xfinity") or {}
    if not box.get("token"):
        raise DeviceError("No Xfinity box is paired with this TV")
    if action == "xfinity_key":
        vcode = xfinity.vcode_for(str(params.get("vcode", "")))
        if not vcode:
            raise ValueError(f"Unknown cable box key: {params.get('vcode')}")
        send = lambda token: xfinity.press(token, vcode)      # noqa: E731
    else:
        text = str(params.get("text", "")).strip()[:200]
        if not text:
            raise ValueError("Type a command, e.g. ESPN or channel 5")
        send = lambda token: xfinity.say(token, text)         # noqa: E731
    if xfinity.needs_refresh(box):
        box = {**box, **await xfinity.refresh(box["token"])}
        _save_box(device_id, box)
    try:
        await send(box["token"])
    except xfinity.Expired:
        box = {**box, **await xfinity.refresh(box["token"])}
        _save_box(device_id, box)
        await send(box["token"])
    return {"sent": params.get("vcode") or params.get("text")}


def _guess_box_port(inputs: list[dict]) -> int | None:
    """HDMI port of the box: a CEC device named like one, else the only CEC tuner."""
    cec = [i for i in inputs if str(i.get("uri", "")).startswith("extInput:cec")]
    named = [i for i in cec if re.search(r"xfinity|x1|comcast|flex|xi\d", str(i.get("title")), re.I)]
    tuners = [i for i in cec if "type=tuner" in i["uri"]]
    pick = named or (tuners if len(tuners) == 1 else [])
    return _input_port(pick[0]["uri"]) if pick else None


@router.post("/{device_id}/xfinity/pair")
async def xfinity_pair(device_id: str):
    """Start pairing: returns the 7-digit code to enter in the box's Easy Pair app."""
    device = _require_device(device_id)
    started = await _run(device, "xfinity_pair", None, lambda: xfinity.pair_start())
    _pairing[device_id] = started
    return {"code": started["code"], "expires": started["expires"]}


@router.post("/{device_id}/xfinity/pair/check")
async def xfinity_pair_check(device_id: str):
    device = _require_device(device_id)
    pending = _pairing.get(device_id)
    if not pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Start pairing first")
    login = await _run(device, "xfinity_pair_check", None,
                       lambda: xfinity.pair_confirm(pending["token"], pending["client_id"]))
    if not login:
        return {"paired": False}
    _pairing.pop(device_id, None)
    port = None
    try:
        port = _guess_box_port((await driver_for(device).action("inputs")).get("inputs", []))
    except DeviceError:
        pass
    old = device["config"].get("xfinity") or {}
    device = _save_box(device_id, {**login, "client_id": pending["client_id"],
                                   "mode": old.get("mode", "auto"), "port": old.get("port") or port})
    debuglog.record(f"Xfinity box paired (HDMI {port or '?'})", kind="device", device=device["name"])
    return {"paired": True, "device": _public(device)}


@router.put("/{device_id}/xfinity")
async def xfinity_settings(device_id: str, body: BoxSettings):
    device = _require_device(device_id)
    box = dict(device["config"].get("xfinity") or {})
    if not box.get("token"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pair the box first")
    if body.mode is not None:
        if body.mode not in ("auto", "box", "off"):
            raise HTTPException(status_code=422, detail="mode must be auto, box or off")
        box["mode"] = body.mode
    if body.port is not None:
        box["port"] = body.port or None
    return {"device": _public(_save_box(device_id, box))}


@router.delete("/{device_id}/xfinity")
async def xfinity_unpair(device_id: str):
    _require_device(device_id)
    _pairing.pop(device_id, None)
    _inputs.pop(device_id, None)
    return {"device": _public(_save_box(device_id, None))}
