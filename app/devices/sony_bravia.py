"""Sony BRAVIA TV driver built on pybravia (the library Home Assistant uses).

Rather than reimplementing Sony's protocol, we depend on pybravia, which encodes
years of hard-won quirks:

  - IRCC "idle wake-up": some TVs ignore the first command after ~10 minutes of
    inactivity, so an empty request is sent first.
  - The IRCC endpoint is case-sensitive on some sets ("ircc" vs "IRCC").
  - Non-RFC-compliant Set-Cookie headers need normalizing.
  - A "not power-on" error maps to a dedicated BraviaTurnedOff exception.
  - IRCC button codes are resolved dynamically from the TV's own command list
    (system.getRemoteControllerInfo) rather than hardcoded.

We keep a thin layer on top so the DeviceDriver interface and our action
vocabulary stay ours, and the device registry stays decoupled from pybravia.
"""

import asyncio
import base64
import binascii
import socket

import aiohttp

from pybravia import (
    BraviaAuthError,
    BraviaClient,
    BraviaError,
    BraviaTurnedOff,
)

from . import adb
from .base import DeviceDriver, DeviceError

# Friendly name -> IRCC command *name* as reported by the TV. These are
# best-effort aliases; exact names vary by model, which is why pybravia resolves
# them against the device's own command list.
COMMAND_ALIASES = {
    "power": "Power",
    "volume_up": "VolumeUp",
    "volume_down": "VolumeDown",
    "mute": "Mute",
    "home": "Home",
    "confirm": "Confirm",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "back": "Return",
    "play": "Play",
    "pause": "Pause",
    "stop": "Stop",
    "next": "Next",
    "prev": "Prev",
    "channel_up": "ChannelUp",
    "channel_down": "ChannelDown",
    "input": "Input",
}

# Friendly input name -> Sony AV content URI.
INPUT_URIS = {
    "tv": "tv:",
    "hdmi1": "extInput:hdmi?port=1",
    "hdmi2": "extInput:hdmi?port=2",
    "hdmi3": "extInput:hdmi?port=3",
    "hdmi4": "extInput:hdmi?port=4",
}


class SonyBravia(DeviceDriver):
    type = "sony_bravia"
    label = "Sony BRAVIA TV"

    def __init__(self, *, host: str, mac: str | None = None, psk: str | None = None,
                 timeout: float = 10.0, **config):
        super().__init__(host=host, mac=mac, **config)
        self.psk = psk
        self.timeout = timeout

    async def _call(self, fn):
        """Run fn(client) against a freshly-connected pybravia client."""
        client = BraviaClient(self.host, self.mac)
        try:
            # connect() authenticates with the PSK and pings the TV. Skipped when
            # the TV has IP-control auth turned off (no PSK configured).
            if self.psk:
                await client.connect(psk=self.psk)
            return await fn(client)
        except BraviaTurnedOff as exc:
            raise DeviceError("TV is turned off") from exc
        except BraviaAuthError as exc:
            raise DeviceError("Authentication failed — check the PSK") from exc
        except BraviaError as exc:
            raise DeviceError(f"{self.host}: {_chain(exc)}") from exc
        finally:
            await client.disconnect()

    # -- Wake-on-LAN (independent of the REST API) -----------------------------

    def _send_wol(self) -> None:
        if not self.mac:
            raise DeviceError("No MAC address configured for Wake-on-LAN")
        mac = normalize_mac(self.mac)
        if not mac:
            raise DeviceError(f"Invalid MAC address: {self.mac!r}")
        packet = bytes.fromhex("F" * 12 + mac.replace(":", "") * 16)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, ("<broadcast>", 9))

    async def wake(self) -> dict:
        await asyncio.to_thread(self._send_wol)
        return {"wake": "sent", "mac": normalize_mac(self.mac)}

    # -- supported remote commands -------------------------------------------------

    async def list_commands(self) -> list[str]:
        """Names of every IRCC remote command this TV supports (its own list)."""
        headers = {"X-Auth-PSK": self.psk} if self.psk else {}
        payload = {"method": "getRemoteControllerInfo", "id": 1, "params": [], "version": "1.0"}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"http://{self.host}/sony/system", json=payload,
                                        headers=headers, allow_redirects=False) as resp:
                    if resp.status in (401, 403):
                        raise DeviceError("Authentication failed — check the PSK")
                    if resp.status != 200:
                        raise DeviceError(f"{self.host} answered HTTP {resp.status} "
                                          "(is this the TV's IP?)")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            raise DeviceError(f"{self.host}: {_chain(exc)}") from exc
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list) or len(result) < 2 or not isinstance(result[1], list):
            raise DeviceError(f"Unexpected reply from TV: {str(data)[:200]}")
        return sorted({c["name"] for c in result[1] if isinstance(c, dict) and c.get("name")})

    # -- TV settings (Sony JSON-RPC) --------------------------------------------------

    async def _rpc(self, service: str, method: str, params: list | None = None,
                   version: str = "1.0"):
        """Call one Sony JSON-RPC method and return its `result` list."""
        headers = {"X-Auth-PSK": self.psk} if self.psk else {}
        payload = {"method": method, "id": 1, "params": params if params is not None else [],
                   "version": version}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"http://{self.host}/sony/{service}", json=payload,
                                        headers=headers, allow_redirects=False) as resp:
                    if resp.status in (401, 403):
                        raise DeviceError("Authentication failed — check the PSK")
                    if resp.status != 200:
                        raise DeviceError(f"{self.host} answered HTTP {resp.status} "
                                          f"for {service}.{method}")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            raise DeviceError(f"{self.host}: {_chain(exc)}") from exc
        if isinstance(data, dict) and "error" in data:
            raise DeviceError(f"{service}.{method}: TV error {data['error']}")
        # getMethodTypes answers with "results" (plural); everything else "result".
        result = None
        if isinstance(data, dict):
            result = data.get("result", data.get("results"))
        if not isinstance(result, list):
            raise DeviceError(f"{service}.{method}: unexpected reply {str(data)[:200]}")
        return result

    async def _rpc_versions(self, service: str, method: str, params: list, versions):
        """Try each API version in turn (models differ); raise the last error."""
        last: DeviceError | None = None
        for version in versions:
            try:
                return await self._rpc(service, method, params, version)
            except DeviceError as exc:
                last = exc
        raise last or DeviceError(f"{service}.{method} failed")

    async def methods(self) -> dict:
        """Which API methods the TV exposes, per service (read-only discovery)."""
        out: dict[str, list[str]] = {}
        for service in ("system", "audio", "videoScreen", "avContent", "appControl"):
            try:
                res = await self._rpc(service, "getMethodTypes", [""], "1.0")
                # "results" is a flat list of [name, request, response, version] rows.
                rows = res if res and isinstance(res[0], list) and res[0] \
                    and isinstance(res[0][0], str) else (res[0] if res else [])
                out[service] = sorted({
                    f"{r[0]} (v{r[3]})" if len(r) > 3 else str(r[0])
                    for r in rows if isinstance(r, list) and r and isinstance(r[0], str)})
            except DeviceError as exc:
                out[service] = [f"(unavailable: {exc})"]
        return {"methods": out}

    async def get_settings(self) -> dict:
        """Picture/sound/system settings the TV lets us change, with their options."""
        items: list[dict] = []
        problems: dict[str, str] = {}

        async def collect(group: str, service: str, methods: tuple[str, ...], versions):
            last = ""
            for method in methods:     # method names differ between models
                try:
                    res = await self._rpc_versions(service, method, [{"target": ""}], versions)
                except DeviceError as exc:
                    last = str(exc)
                    continue
                for row in res[0] if res and isinstance(res[0], list) else []:
                    if isinstance(row, dict) and row.get("target"):
                        items.append(_setting_item(group, row))
                return
            problems[group] = last

        await collect("picture", "videoScreen", ("getPictureQualitySettings",), ("1.0",))
        await collect("sound", "audio", ("getSoundSettings", "getSpeakerSettings"), ("1.1", "1.0"))

        # HDMI-control / remote-device options, where the model exposes them.
        await collect("remote", "system", ("getRemoteDeviceSettings",), ("1.0",))

        try:   # "Remote start" (Wake-on-LAN) on/off
            res = await self._rpc("system", "getWolMode", [], "1.0")
            if res and isinstance(res[0], dict) and "enabled" in res[0]:
                items.append({"group": "system", "target": "wolMode", "kind": "toggle",
                              "title": "Remote start (Wake-on-LAN)", "available": True,
                              "value": "on" if res[0]["enabled"] else "off"})
        except DeviceError as exc:
            problems["wol"] = str(exc)

        try:
            res = await self._rpc("system", "getPowerSavingMode", [], "1.0")
            mode = res[0].get("mode") if res and isinstance(res[0], dict) else None
            if mode is not None:
                items.append({"group": "system", "target": "powerSavingMode", "value": mode,
                              "kind": "choice", "available": True,
                              "options": ["off", "low", "high", "pictureOff"]})
        except DeviceError as exc:
            problems["system"] = str(exc)
        return {"settings": items, "problems": problems}

    async def set_setting(self, params: dict) -> dict:
        group, target = params.get("group"), params.get("target")
        value = params.get("value")
        if group not in ("picture", "sound", "remote", "system") or not target or value is None:
            raise ValueError("set_setting needs group (picture|sound|remote|system), target and value")
        value = str(value)
        if group == "remote":
            await self._rpc("system", "setRemoteDeviceSettings",
                            [{"settings": [{"target": target, "value": value}]}], "1.0")
        elif group == "picture":
            await self._rpc("videoScreen", "setPictureQualitySettings",
                            [{"settings": [{"target": target, "value": value}]}], "1.0")
        elif group == "sound":
            body = [{"settings": [{"target": target, "value": value}]}]
            try:
                await self._rpc_versions("audio", "setSoundSettings", body, ("1.1", "1.0"))
            except DeviceError:      # some models only have the "Speaker" variant
                await self._rpc("audio", "setSpeakerSettings", body, "1.0")
        else:
            if target == "wolMode":
                await self._rpc("system", "setWolMode", [{"enabled": value == "on"}], "1.0")
            elif target == "powerSavingMode":
                await self._rpc("system", "setPowerSavingMode", [{"mode": value}], "1.0")
            else:
                raise ValueError(f"Unknown system setting: {target}")
        return {"ok": True, "group": group, "target": target, "value": value}

    # -- MAC address lookup -------------------------------------------------------

    def _arp_lookup(self) -> str | None:
        """Look the TV's IP up in the kernel ARP table (same L2 network only)."""
        try:
            with open("/proc/net/arp") as fh:
                for line in fh.read().splitlines()[1:]:
                    cols = line.split()
                    if len(cols) >= 4 and cols[0] == self.host:
                        mac = normalize_mac(cols[3])
                        if mac and mac != "00:00:00:00:00:00":
                            return mac
        except OSError:
            pass
        return None

    async def fetch_mac(self) -> str | None:
        """Ask the TV for its MAC (preferring wired), else fall back to ARP."""
        candidates: dict[str, str] = {}
        info = await self._call(lambda c: c.get_system_info())
        for key, value in (info or {}).items():
            if "mac" in key.lower():
                mac = normalize_mac(value)
                if mac:
                    candidates[key.lower()] = mac
        # Wired MAC is what Wake-on-LAN needs; "macAddr" is Sony's wired field.
        for key in ("macaddr", "wiredmac", "ethernetmac"):
            if key in candidates:
                return candidates[key]
        if candidates:
            return next(iter(candidates.values()))
        return await asyncio.to_thread(self._arp_lookup)

    # -- public interface -------------------------------------------------------

    async def status(self) -> dict:
        try:
            power = await self._call(lambda c: c.get_power_status())
        except DeviceError as exc:
            # A TV in deep standby is unreachable; report it as off rather than
            # raising, but keep the reason so the debug log can show it.
            return {"power": "off", "volume": None, "mute": False, "error": _reason(exc)}
        info: dict = {}
        if power == "active":
            try:
                vol = await self._call(lambda c: c.get_volume_info())
                info = {
                    "volume": _as_int(vol.get("volume")),
                    "mute": bool(vol.get("mute")),
                }
            except DeviceError as exc:
                info = {"volume": None, "mute": False, "error": _reason(exc)}
        return {"power": power, **info}

    async def action(self, name: str, params: dict | None = None) -> dict:
        params = params or {}
        if name == "status":
            return await self.status()
        if name == "wake":
            return await self.wake()
        if name == "commands":
            return {"commands": await self.list_commands()}
        if name == "fetch_mac":
            return {"mac": await self.fetch_mac()}
        if name == "adb_info":
            port = params.get("port")
            return await adb.device_info(self.host, int(port) if port else None)
        if name == "adb_supported":
            port = params.get("port")
            return await adb.supported_pages(self.host, int(port) if port else None)
        if name == "adb_activities":
            port = params.get("port")
            return await adb.list_activities(self.host, int(port) if port else None)
        if name == "adb_open":
            port = params.get("port")
            return await adb.open_settings(self.host, params.get("page"), params.get("intent"),
                                           int(port) if port else None)
        if name == "methods":
            return await self.methods()
        if name == "settings":
            return await self.get_settings()
        if name == "set_setting":
            return await self.set_setting(params)
        if name == "power_on":
            return {"turned_on": await self._call(lambda c: c.turn_on())}
        if name == "power_off":
            return {"turned_off": await self._call(lambda c: c.turn_off())}
        if name == "toggle_power":
            power = (await self.status()).get("power")
            return await self.action("power_off" if power == "active" else "power_on")
        if name == "set_volume":
            volume = int(params.get("volume", 0))
            return {"ok": await self._call(lambda c: c.volume_level(volume))}
        if name == "volume_up":
            return {"ok": await self._call(lambda c: c.volume_up(int(params.get("step", 1))))}
        if name == "volume_down":
            return {"ok": await self._call(lambda c: c.volume_down(int(params.get("step", 1))))}
        if name == "set_mute":
            return {"ok": await self._call(lambda c: c.volume_mute(bool(params.get("mute", True))))}
        if name == "toggle_mute":
            return {"ok": await self._call(lambda c: c.volume_mute(None))}
        if name == "set_input":
            uri = params.get("uri") or INPUT_URIS.get(
                str(params.get("input", "tv")).lower(), str(params.get("input", "tv")))
            return {"ok": await self._call(lambda c: c.set_play_content(uri))}
        if name == "apps":
            return {"apps": await self._call(lambda c: c.get_app_list())}
        if name == "launch_app":
            uri = params.get("uri")
            if not uri:
                uri = await self._app_uri(params.get("title", ""))
            return {"ok": await self._call(lambda c: c.set_active_app(uri))}
        if name == "ircc":
            return await self._ircc(params)
        if name == "system_info":
            return await self._call(lambda c: c.get_system_info())
        if name == "playing":
            return await self._call(lambda c: c.get_playing_info())
        if name == "terminate_apps":
            return {"ok": await self._call(lambda c: c.terminate_apps())}
        raise DeviceError(f"Unknown action: {name}")

    async def _ircc(self, params: dict) -> dict:
        code = str(params.get("code", "home"))
        if _looks_b64(code):
            ok = await self._call(lambda c: c.send_ircc_req(code))
        else:
            command = COMMAND_ALIASES.get(code, code)
            ok = await self._call(lambda c: c.send_command(command))
        return {"ircc": code, "ok": ok}

    async def _app_uri(self, title: str) -> str:
        apps = await self._call(lambda c: c.get_app_list())
        for app in apps:
            if str(app.get("title", "")).lower() == title.lower():
                return app["uri"]
        raise DeviceError(f"App not found: {title}")


def _setting_item(group: str, row: dict) -> dict:
    """Normalize one Sony setting row into {group,target,value,kind,options|min/max/step}."""
    item = {"group": group, "target": row["target"], "value": row.get("currentValue"),
            "available": row.get("isAvailable", True) is not False,
            "title": row.get("title") or ""}
    cands = [c for c in row.get("candidate") or [] if isinstance(c, dict)]
    ranged = next((c for c in cands if "min" in c and "max" in c), None)
    if ranged:
        item.update(kind="range", min=_as_int(ranged["min"]), max=_as_int(ranged["max"]),
                    step=_as_int(ranged.get("step")) or 1)
    else:
        options = [str(c["value"]) for c in cands if "value" in c and c.get("isAvailable", True)]
        item.update(kind="choice", options=options)
        if set(options) == {"on", "off"}:
            item["kind"] = "toggle"
    return item


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_b64(value: str) -> bool:
    # IRCC codes are base64 of 12+ bytes (16+ chars); command names are shorter.
    if len(value) < 16:
        return False
    try:
        base64.b64decode(value)
        return True
    except (binascii.Error, ValueError):
        return False


def normalize_mac(value) -> str | None:
    """Return 'aa:bb:cc:dd:ee:ff' for any common MAC spelling, else None."""
    digits = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if len(digits) != 12 or any(ch not in "0123456789abcdef" for ch in digits):
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _chain(exc: BaseException) -> str:
    """Describe an exception and everything that caused it, e.g.
    "BraviaConnectionError <- ClientConnectorError: Cannot connect to host ..."."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {text}" if text else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


def _reason(exc: BaseException) -> str:
    """Error text plus the underlying library exception, for the debug log."""
    cause = exc.__cause__
    return f"{exc} ({_chain(cause)})" if cause else str(exc)
