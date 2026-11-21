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

from pybravia import (
    BraviaAuthError,
    BraviaClient,
    BraviaError,
    BraviaTurnedOff,
)

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
            raise DeviceError(str(exc)) from exc
        finally:
            await client.disconnect()

    # -- Wake-on-LAN (independent of the REST API) -----------------------------

    def _send_wol(self) -> None:
        if not self.mac:
            raise DeviceError("No MAC address configured for Wake-on-LAN")
        mac = self.mac.replace(":", "")
        packet = bytes.fromhex("F" * 12 + mac * 16)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, ("<broadcast>", 9))

    async def wake(self) -> dict:
        await asyncio.to_thread(self._send_wol)
        return {"wake": "sent"}

    # -- public interface -------------------------------------------------------

    async def status(self) -> dict:
        try:
            power = await self._call(lambda c: c.get_power_status())
        except DeviceError:
            # A TV in deep standby is unreachable; report it as off rather than
            # surfacing a connection error.
            return {"power": "off", "volume": None, "mute": False}
        info: dict = {}
        if power == "active":
            try:
                vol = await self._call(lambda c: c.get_volume_info())
                info = {
                    "volume": _as_int(vol.get("volume")),
                    "mute": bool(vol.get("mute")),
                }
            except DeviceError:
                info = {"volume": None, "mute": False}
        return {"power": power, **info}

    async def action(self, name: str, params: dict | None = None) -> dict:
        params = params or {}
        if name == "status":
            return await self.status()
        if name == "wake":
            return await self.wake()
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
