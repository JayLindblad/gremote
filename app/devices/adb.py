"""Android Debug Bridge access to a Google TV (the "Network debugging" option).

Used for things Sony's REST API cannot reach, such as opening Google TV Settings pages.
gremote keeps one RSA key; the TV asks to trust it the first time we connect.
"""

import asyncio
import os
from pathlib import Path

from ..config import settings
from .base import DeviceError

ADB_PORT = 5555
CONNECT_TIMEOUT = 25.0   # leaves time to tap "Allow" on the TV the first time


def key_path() -> Path:
    return Path(settings.database_path).resolve().parent / "adbkey"


def _signer():
    from adb_shell.auth.keygen import keygen
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner

    path = key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return PythonRSASigner(Path(f"{path}.pub").read_text(), path.read_text())


class AdbSession:
    """async with AdbSession(host) as adb: await adb.shell("...")"""

    def __init__(self, host: str, port: int = ADB_PORT):
        self.host, self.port = host, port
        self._dev = None

    async def __aenter__(self):
        try:
            from adb_shell.adb_device_async import AdbDeviceTcpAsync
        except ImportError as exc:
            raise DeviceError("adb-shell is not installed; run `update` to install it") from exc
        signer = await asyncio.to_thread(_signer)
        dev = AdbDeviceTcpAsync(self.host, self.port, default_transport_timeout_s=10.0)
        try:
            await dev.connect(rsa_keys=[signer], auth_timeout_s=CONNECT_TIMEOUT)
        except ConnectionRefusedError as exc:
            raise DeviceError(
                f"{self.host}:{self.port} refused the connection. Turn on Network debugging "
                "on the TV (Developer options)") from exc
        except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
            raise DeviceError(
                f"{self.host}:{self.port} did not answer in time. If the TV is showing "
                "'Allow USB debugging?', choose Always allow, then try again "
                f"({type(exc).__name__})") from exc
        except Exception as exc:   # adb_shell has its own exception types
            raise DeviceError(f"ADB connect failed: {type(exc).__name__}: {exc}") from exc
        self._dev = dev
        return self

    async def __aexit__(self, *exc):
        if self._dev is not None:
            try:
                await self._dev.close()
            except Exception:
                pass

    async def shell(self, command: str) -> str:
        try:
            return (await self._dev.shell(command, read_timeout_s=10.0)).strip()
        except Exception as exc:
            raise DeviceError(f"ADB command failed: {type(exc).__name__}: {exc}") from exc


# -- port discovery ------------------------------------------------------------------
# Classic "Network debugging" listens on 5555. Android's newer wireless debugging picks
# a random port each time it is switched on (30000-50000), so we look for it.
SCAN_RANGE = range(30000, 50001)
SCAN_CONCURRENCY = 800
SCAN_PROBE_TIMEOUT = 0.6
MAX_CANDIDATES = 4

_known_port: dict[str, int] = {}    # host -> last port that worked


async def _is_open(host: str, port: int, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port),
                                               SCAN_PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True


async def scan_ports(host: str) -> list[int]:
    """Open TCP ports in the wireless-debugging range, lowest first."""
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    ports = list(SCAN_RANGE)
    hits = await asyncio.gather(*(_is_open(host, p, sem) for p in ports))
    return [p for p, ok in zip(ports, hits) if ok]


async def find_candidates(host: str, port: int | None = None) -> list[int]:
    if port:
        return [port]
    first = [p for p in (_known_port.get(host), ADB_PORT) if p]
    ordered: list[int] = []
    for p in first:
        if p not in ordered and await _is_open(host, p, asyncio.Semaphore(1)):
            ordered.append(p)
    if not ordered:
        ordered = (await scan_ports(host))[:MAX_CANDIDATES]
    return ordered


async def connect_any(host: str, port: int | None = None, log=None):
    """Return a connected AdbSession on whichever port the TV is listening on."""
    candidates = await find_candidates(host, port)
    if not candidates:
        raise DeviceError(
            f"{host}: no ADB port found (checked {ADB_PORT} and "
            f"{SCAN_RANGE.start}-{SCAN_RANGE.stop - 1}). Turn on Network/Wireless debugging "
            "on the TV and make sure it is awake")
    errors = []
    for candidate in candidates:
        session = AdbSession(host, candidate)
        try:
            await session.__aenter__()
        except DeviceError as exc:
            errors.append(f"port {candidate}: {exc}")
            continue
        _known_port[host] = candidate
        session.port = candidate
        return session
    raise DeviceError("; ".join(errors))


async def device_info(host: str, port: int | None = None) -> dict:
    session = await connect_any(host, port)
    try:
        props = {
            "manufacturer": "ro.product.manufacturer",
            "model": "ro.product.model",
            "android": "ro.build.version.release",
            "sdk": "ro.build.version.sdk",
            "build": "ro.build.display.id",
        }
        info = {k: await session.shell(f"getprop {p}") for k, p in props.items()}
        info["adb_port"] = session.port
        return info
    finally:
        await session.__aexit__()


# -- Settings shortcuts ----------------------------------------------------------------
# key -> Android settings intent action. Most route to the matching Google TV page.
SETTINGS_PAGES = {
    "settings": "android.settings.SETTINGS",
    "network": "android.settings.WIFI_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "sound": "android.settings.SOUND_SETTINGS",
    "apps": "android.settings.APPLICATION_SETTINGS",
    "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "datetime": "android.settings.DATE_SETTINGS",
    "language": "android.settings.LOCALE_SETTINGS",
    "keyboard": "android.settings.INPUT_METHOD_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "privacy": "android.settings.PRIVACY_SETTINGS",
    "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "about": "android.settings.DEVICE_INFO_SETTINGS",
    "developer": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
}

_SAFE_INTENT = __import__("re").compile(r"^[A-Za-z0-9_.:/$-]{3,200}$")


async def open_settings(host: str, page: str | None = None, intent: str | None = None,
                        port: int | None = None) -> dict:
    """Open a Settings page on the TV by shortcut key or a custom intent action."""
    action = SETTINGS_PAGES.get(page or "") or intent
    if not action or not _SAFE_INTENT.match(action):
        raise ValueError("Unknown settings page, or invalid intent action")
    session = await connect_any(host, port)
    try:
        out = await session.shell(f"am start -a {action}")
    finally:
        await session.__aexit__()
    lowered = out.lower()
    if "error" in lowered or "unable to resolve" in lowered or "exception" in lowered:
        raise DeviceError(f"TV could not open {action}: {out[:300]}")
    return {"opened": action, "adb_port": session.port}
