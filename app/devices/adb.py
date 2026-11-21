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


async def device_info(host: str, port: int = ADB_PORT) -> dict:
    props = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "android": "ro.build.version.release",
        "sdk": "ro.build.version.sdk",
        "build": "ro.build.display.id",
    }
    async with AdbSession(host, port) as adb:
        return {k: await adb.shell(f"getprop {p}") for k, p in props.items()}
