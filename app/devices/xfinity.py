"""Xfinity X1 / Flex box control through Comcast's Adaptive Remote cloud service.

X1 boxes ignore remote keys sent over HDMI-CEC and offer no open local API, but Comcast's
accessibility web remote (https://accrem.apps.cloud.comcast.net) presses keys on a box
over the internet. gremote talks to the same service:

  1. pairing/start            -> a 7-digit code (and a short-lived pairing token)
  2. On the TV: say "Easy Pair" into the voice remote, enter the code, pick Pair Now.
  3. pairing/confirm (polled) -> an arToken for that box
  4. processKey {vcode}       -> presses a key; text {cmd} -> a typed voice command
  5. auth/token/refresh       -> a fresh arToken (the service says how often)

The box is attached to a TV in gremote: when the TV is showing the box's HDMI input,
navigation/channel/number/playback keys go to the box instead of the TV.
"""

import time
import uuid

import aiohttp

from .base import DeviceError

BASE = "https://accrem.apps.cloud.comcast.net/api/v1"
PARTNER = "comcast"
TIMEOUT = aiohttp.ClientTimeout(total=10)

# gremote remote key -> Adaptive Remote vcode. Keys not listed stay with the TV
# (power, volume, mute, home, input, options...).
KEYS = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "confirm": "ENTER", "back": "PREV",
    "channel_up": "CHANNEL_UP", "channel_down": "CHANNEL_DOWN",
    "play": "PLAY", "pause": "PLAY", "rewind": "REWIND", "forward": "FAST_FORWARD",
    "next": "FAST_FORWARD", "prev": "REWIND",
    "num0": "NUMBER_0", "num1": "NUMBER_1", "num2": "NUMBER_2", "num3": "NUMBER_3",
    "num4": "NUMBER_4", "num5": "NUMBER_5", "num6": "NUMBER_6", "num7": "NUMBER_7",
    "num8": "NUMBER_8", "num9": "NUMBER_9",
    "display": "INFO", "epg": "GUIDE", "gguide": "GUIDE", "exit": "EXIT",
    # X1 A/B/C/D are yellow/blue/red/green.
    "yellow": "COLOR_KEY_0", "blue": "COLOR_KEY_1", "red": "COLOR_KEY_2", "green": "COLOR_KEY_3",
}
# Box-only buttons (the cable box panel sends these directly).
VCODES = sorted(set(KEYS.values()) | {
    "MENU", "PAGE_UP", "PAGE_DOWN", "RECORD", "REPLAY", "SHORTCUT"})

DEFAULT_REFRESH_MS = 30 * 60 * 1000


def vcode_for(key: str) -> str | None:
    """The box key for a gremote/IRCC key name, or None if it belongs to the TV."""
    k = str(key).strip()
    if k in VCODES:
        return k
    return KEYS.get(k.lower())


async def _post(path: str, body: dict | None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as http:
            async with http.post(f"{BASE}/{path}", json=body, headers=headers) as res:
                try:
                    data = await res.json(content_type=None)
                except ValueError:
                    data = {}
                return res.status, data if isinstance(data, dict) else {}
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise DeviceError(f"Xfinity service unreachable: {exc or type(exc).__name__}") from exc


async def pair_start(client_id: str | None = None) -> dict:
    client_id = client_id or str(uuid.uuid4())
    code, data = await _post("pairing/start", {"partner": PARTNER, "clientDeviceId": client_id})
    if code != 200 or not data.get("pairingCode") or not data.get("token"):
        raise DeviceError(f"Xfinity pairing could not start (HTTP {code}: {data.get('status', '')})")
    return {"client_id": client_id, "code": data["pairingCode"], "token": data["token"],
            "expires": data.get("exp")}


async def pair_confirm(pair_token: str, client_id: str) -> dict | None:
    """The new login once the code was entered on the TV; None while still waiting."""
    code, data = await _post("pairing/confirm", {"partner": PARTNER, "clientDeviceId": client_id},
                             pair_token)
    if code == 202 or (code == 200 and data.get("status") != "CONFIRMED"):
        return None
    if code != 200 or not data.get("arToken"):
        raise DeviceError(f"Xfinity pairing failed (HTTP {code}: {data.get('status', '')})")
    return _login(data)


def _login(data: dict) -> dict:
    return {"token": data["arToken"],
            "refresh_ms": int(data.get("refreshIntervalMs") or DEFAULT_REFRESH_MS),
            "refreshed": time.time()}


async def refresh(token: str) -> dict:
    code, data = await _post("auth/token/refresh", None, token)
    if code == 200 and data.get("arToken"):
        return _login(data)
    if code == 401:
        raise DeviceError("The Xfinity box is no longer paired. Pair it again in Cable box.")
    raise DeviceError(f"Xfinity sign-in refresh failed (HTTP {code}: {data.get('status', '')})")


def needs_refresh(login: dict) -> bool:
    age_ms = (time.time() - float(login.get("refreshed") or 0)) * 1000
    return age_ms > int(login.get("refresh_ms") or DEFAULT_REFRESH_MS) * 0.8


async def press(token: str, vcode: str) -> None:
    code, data = await _post("processKey", {"vcode": vcode}, token)
    _check(code, data, "press that key")


async def say(token: str, text: str) -> None:
    # The web remote sends this one with the token in the body, not a header.
    code, data = await _post("text", {"cmd": text, "arToken": token})
    _check(code, data, "send that command")


class Expired(DeviceError):
    """The arToken was refused; refresh and try once more."""


def _check(code: int, data: dict, what: str) -> None:
    if code == 200:
        return
    reason = str(data.get("status") or "")
    if code == 401 or reason.startswith("EXPIRED"):
        raise Expired(f"Xfinity sign-in expired ({reason or code})")
    if reason == "INVALID-KEY":
        raise DeviceError("The Xfinity box does not accept that key")
    raise DeviceError(f"Xfinity box could not {what} (HTTP {code}{': ' + reason if reason else ''})")
