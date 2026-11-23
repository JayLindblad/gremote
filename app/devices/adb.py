"""Android Debug Bridge access to an Android TV / Google TV (the "Network debugging" option).

Used for things Sony's REST API cannot reach, such as opening the TV's Settings pages.
Works back to Android TV 9 (e.g. BRAVIA A1E): plain "Network debugging" on port 5555.
gremote keeps one RSA key; the TV asks to trust it the first time we connect.
"""

import asyncio
import base64
import os
import re
import time
from pathlib import Path

from ..config import settings
from . import android_pages
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

    async def exec_bytes(self, command: str, timeout: float = 10.0) -> bytes:
        """Run a command over adb's exec: service (no pty) and return its raw output."""
        try:
            return await self._dev.exec_out(command, read_timeout_s=timeout,
                                            transport_timeout_s=timeout, decode=False)
        except Exception as exc:
            raise DeviceError(f"ADB command failed: {type(exc).__name__}: {exc}") from exc

    async def shell(self, command: str, timeout: float = 10.0) -> str:
        try:
            return (await self._dev.shell(command, read_timeout_s=timeout,
                                          transport_timeout_s=timeout)).strip()
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


# -- Settings pages ------------------------------------------------------------------------
# Which pages a TV has is worked out on the TV itself by android_pages.scan (no per-model
# lists). Without a scan, a shortcut falls back to its standard Android actions.
SETTINGS_PAGES = {key: actions for key, (actions, _) in android_pages.SHORTCUTS.items()}
_SAFE_INTENT = re.compile(r"^[A-Za-z0-9_.:/$-]{3,200}$")


async def open_settings(host: str, page: str | None = None, intent: str | None = None,
                        port: int | None = None) -> dict:
    """Open a Settings page on the TV by a target from the scan, a shortcut key or an action."""
    targets = [intent] if intent else SETTINGS_PAGES.get(page or "", [])
    if not targets or not all(_SAFE_INTENT.match(t) for t in targets):
        raise ValueError("Unknown settings page, or invalid intent action")
    session = await connect_any(host, port)
    errors = []
    try:
        for target in targets:
            if not intent and not await android_pages.usable(session, target):
                errors.append(f"{target}: not on this TV")
                continue
            out = await session.shell(f"am start {android_pages._flag(target)} '{target}'")
            lowered = out.lower()
            if "error" in lowered or "unable to resolve" in lowered or "exception" in lowered:
                errors.append(f"{target}: {out[:300]}")
                continue
            return {"opened": target, "adb_port": session.port}
    finally:
        await session.__aexit__()
    raise DeviceError("TV could not open " + "; ".join(errors))


async def scan_pages(host: str, port: int | None = None) -> dict:
    """Which shortcuts work on this TV (and what each opens), plus every other Settings page."""
    session = await connect_any(host, port)
    try:
        result = await android_pages.scan(session)
        result["adb_port"] = session.port
        return result
    finally:
        await session.__aexit__()


# -- screen reader / accessibility services --------------------------------------------------
# Turned-on accessibility services are listed in Settings.Secure, which ADB may read and write
# on any Android TV. A screen reader is recognised by its name (Sony's ScreenReaderService,
# Google's TalkBack, ...), so no model list is needed.
_SCREEN_READER = re.compile(r"screen.?reader|talkback|marvin|spoken|voiceassistant|voice.?view",
                            re.I)
_SAFE_COMPONENT = re.compile(r"^[\w.]+/[\w.$]+$")


def _service_name(component: str) -> str:
    if re.search(r"talkback|marvin", component, re.I):
        return "TalkBack"
    if re.search(r"screen.?reader", component, re.I):
        return "Screen reader"
    cls = component.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return re.sub(r"Service$", "", cls) or component


async def _accessibility(session) -> dict:
    raw = (await session.shell("settings get secure enabled_accessibility_services")).strip()
    on = (await session.shell("settings get secure accessibility_enabled")).strip() == "1"
    services = [c for c in raw.split(":") if c and c != "null"]
    listed = [{"component": c, "name": _service_name(c),
               "screen_reader": bool(_SCREEN_READER.search(c))} for c in services]
    return {"enabled": on and bool(services), "services": listed,
            "screen_reader_on": on and any(s["screen_reader"] for s in listed)}


async def accessibility_status(host: str, port: int | None = None) -> dict:
    """Which accessibility services (e.g. a screen reader) are turned on."""
    session = await connect_any(host, port)
    try:
        return await _accessibility(session)
    finally:
        await session.__aexit__()


async def screen_reader_off(host: str, everything: bool = False, port: int | None = None) -> dict:
    """Turn off the screen reader (or every accessibility service), then report the new state."""
    session = await connect_any(host, port)
    try:
        before = await _accessibility(session)
        keep = [s["component"] for s in before["services"]
                if not everything and not s["screen_reader"]]
        if any(not _SAFE_COMPONENT.match(c) for c in keep):
            raise DeviceError("Unexpected accessibility service name on the TV")
        if keep:
            await session.shell("settings put secure enabled_accessibility_services '"
                                + ":".join(keep) + "'")
        else:
            await session.shell("settings put secure enabled_accessibility_services ''")
            await session.shell("settings put secure accessibility_enabled 0")
        after = await _accessibility(session)
        if after["screen_reader_on"] or (everything and after["enabled"]):
            raise DeviceError("The TV kept its screen reader on; turn it off in Settings > "
                              "Accessibility")
        turned_off = [s["name"] for s in before["services"] if s["component"] not in keep]
        return {**after, "turned_off": turned_off}
    finally:
        await session.__aexit__()


# -- on-screen messages ------------------------------------------------------------------------
# Android TV has no shell command that puts text on screen (`cmd notification post` is Android
# 10+, and TVs don't pop notifications up anyway). So gremote installs a tiny app of its own
# (android/messenger) and starts it with the message. It draws over whatever is playing,
# which needs the "display over other apps" app-op, granted here over ADB.
MESSENGER_PKG = "com.gremote.messenger"
MESSENGER_APK = Path(__file__).with_name("gremote-messenger.apk")
# Written by android/messenger/build-in-docker.sh alongside the APK (its android:versionCode).
MESSENGER_VERSION = int(Path(__file__).with_name("gremote-messenger.version").read_text())
MESSAGE_MAX = 500
_REMOTE_APK = "/data/local/tmp/gremote-messenger.apk"


async def _messenger_version(session) -> int | None:
    dump = await session.shell(f"dumpsys package {MESSENGER_PKG} | grep versionCode")
    m = re.search(r"versionCode=(\d+)", dump)
    return int(m.group(1)) if m else None


async def _ensure_messenger(session) -> bool:
    """Install (or upgrade) the messenger app if needed; return True if it was installed now."""
    installed = await _messenger_version(session)
    if installed is None or installed < MESSENGER_VERSION:
        if not MESSENGER_APK.exists():
            raise DeviceError(f"{MESSENGER_APK.name} is missing from this gremote install")
        try:
            await session._dev.push(str(MESSENGER_APK), _REMOTE_APK, read_timeout_s=30.0,
                                    transport_timeout_s=30.0)
        except Exception as exc:
            raise DeviceError(f"Could not copy the message app to the TV: {exc}") from exc
        out = await session.shell(f"pm install -r {_REMOTE_APK}", timeout=60.0)
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:   # older copy signed by another build
            await session.shell(f"pm uninstall {MESSENGER_PKG}", timeout=30.0)
            out = await session.shell(f"pm install {_REMOTE_APK}", timeout=60.0)
        await session.shell(f"rm -f {_REMOTE_APK}")
        if "Success" not in out:
            raise DeviceError(f"The TV would not install the message app: {out[:300]}")
    await session.shell(f"appops set {MESSENGER_PKG} SYSTEM_ALERT_WINDOW allow")
    return installed is None or installed < MESSENGER_VERSION


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


async def show_message(host: str, text: str, title: str = "", mode: str = "banner",
                       seconds: int = 10, port: int | None = None) -> dict:
    """Show text on the TV: a banner over the video, or full screen until a key is pressed."""
    text, title = text.strip(), title.strip()
    if not text:
        raise ValueError("Type a message first")
    if len(text) > MESSAGE_MAX or len(title) > 100:
        raise ValueError(f"Message too long (max {MESSAGE_MAX} characters)")
    if mode not in ("banner", "full"):
        raise ValueError("mode must be banner or full")
    seconds = max(0, min(int(seconds), 600))
    session = await connect_any(host, port)
    try:
        installed = await _ensure_messenger(session)
        # Base64 is shell-safe, so no quoting worries whatever the message says.
        extras = f"--es text {_b64(text)} --es mode {mode} --ei seconds {seconds}"
        if title:
            extras += f" --es title {_b64(title)}"
        out = await session.shell(
            f"am start-foreground-service -n {MESSENGER_PKG}/.MessageService {extras}")
        if "error" in out.lower() or "exception" in out.lower():
            raise DeviceError(f"The TV would not show the message: {out[:300]}")
        return {"shown": True, "mode": mode, "seconds": seconds,
                "installed_app": installed, "adb_port": session.port}
    finally:
        await session.__aexit__()


async def clear_message(host: str, port: int | None = None) -> dict:
    session = await connect_any(host, port)
    try:
        if await _messenger_version(session) is not None:
            await session.shell(f"am start-foreground-service -n {MESSENGER_PKG}/.MessageService"
                                " --ez clear true")
        return {"cleared": True}
    finally:
        await session.__aexit__()


async def list_activities(host: str, port: int | None = None) -> dict:
    """Activities of the TV's Settings packages, to find the right page to open."""
    session = await connect_any(host, port)
    try:
        packages = await session.shell("pm list packages")
        names = sorted({line.split(":", 1)[1].strip() for line in packages.splitlines()
                        if line.startswith("package:")
                        and re.search(r"settings|tv\.settings", line, re.I)})
        found: dict[str, list[str]] = {}
        for pkg in names[:6]:
            dump = await session.shell(f"dumpsys package {pkg}")
            acts = {m.group(1) for m in re.finditer(rf"\b({re.escape(pkg)}/[\w.$]+)", dump)}
            found[pkg] = sorted(acts)
            # The resolver table maps each intent action to the activities that answer it.
            for action, comp in re.findall(
                    rf'^\s+([A-Za-z][\w.]*):\s*\n\s+\w+\s+({re.escape(pkg)}/[\w.$]+)', dump, re.M):
                found[pkg].append(f"[handles {action}] {comp}")
        return {"packages": found, "adb_port": session.port}
    finally:
        await session.__aexit__()


# -- screen preview (prototype) -----------------------------------------------------------
# Frames are `screencap -p` PNGs, base64-encoded so the shell channel cannot mangle bytes.
# One ADB session per TV is kept open between frames; it is dropped after a short idle time.
SCREEN_IDLE_SECONDS = 30.0
_screen: dict[str, dict] = {}   # host -> {"lock", "session", "last"}


async def _screen_close(entry: dict) -> None:
    session, entry["session"] = entry.get("session"), None
    if session is not None:
        await session.__aexit__()


async def _screen_reaper(host: str) -> None:
    while True:
        await asyncio.sleep(SCREEN_IDLE_SECONDS / 2)
        entry = _screen.get(host)
        if not entry:
            return
        if time.monotonic() - entry["last"] > SCREEN_IDLE_SECONDS:
            async with entry["lock"]:
                await _screen_close(entry)
            _screen.pop(host, None)
            return


async def screenshot(host: str, port: int | None = None) -> bytes:
    """One PNG frame of what the TV is showing (protected video such as Netflix shows black)."""
    entry = _screen.get(host)
    if entry is None:
        entry = _screen[host] = {"lock": asyncio.Lock(), "session": None, "last": time.monotonic()}
        asyncio.create_task(_screen_reaper(host))
    async with entry["lock"]:
        for attempt in (1, 2):
            if entry["session"] is None:
                entry["session"] = await connect_any(host, port)
            try:
                out = await entry["session"].shell("screencap -p | base64 -w 0", timeout=20.0)
                png = base64.b64decode(out, validate=False)
                if not png.startswith(b"\x89PNG"):
                    # No usable base64 on this TV: fetch the PNG over exec (no pty, binary-safe).
                    png = await entry["session"].exec_bytes("screencap -p", timeout=20.0)
                if not png.startswith(b"\x89PNG"):
                    raise DeviceError(f"screencap did not return an image: {out[:120]}")
                entry["last"] = time.monotonic()
                return png
            except DeviceError:
                await _screen_close(entry)      # reconnect once, then give up
                if attempt == 2:
                    raise
            except ValueError as exc:
                await _screen_close(entry)
                raise DeviceError(f"Could not decode the screenshot: {exc}") from exc
    raise DeviceError("screenshot failed")


async def _raise_encoder_error(session, size: str, exc) -> None:
    """The stream produced nothing: surface what screenrecord said on the TV, if anything."""
    try:
        said = await session.shell("head -c 600 /data/local/tmp/gremote-sr.err")
    except DeviceError:
        said = ""
    if said:
        raise DeviceError(f"The TV's screenrecord failed at {size}: {said}")
    if exc is None:
        raise DeviceError(f"The TV's screenrecord ended without producing video at {size}")


async def _raw_pty_ok(session) -> bool:
    """Can binary data cross this shell channel untouched once the TV's pty post-processing is off?

    Writes 64 KB of random bytes on the TV, reads them back with `stty -opost`, and compares
    checksums. If this passes, the video needs no base64 detour (no 4 KB output batching,
    a third less data).
    """
    import hashlib

    path = "/data/local/tmp/gremote-raw.bin"
    try:
        await session.shell(f"head -c 65536 /dev/urandom > {path}", timeout=15.0)
        want = (await session.shell(f"md5sum < {path}", timeout=15.0)).split()[:1]
        digest, size = hashlib.md5(), 0
        async for chunk in session._dev.streaming_shell(
                f"stty -opost; cat {path}", transport_timeout_s=10.0, read_timeout_s=10.0,
                decode=False):
            digest.update(chunk)
            size += len(chunk)
        return bool(want) and digest.hexdigest() == want[0] and size == 65536
    except Exception:
        return False


# How long the relay waits for more bytes before treating the tail as a whole frame.
# Raw mode delivers a frame within milliseconds (small frames come every ~25 ms at 640x360);
# base64 mode arrives in ~4 KB batches. Keyframes and large frames get longer (see the relay).
QUIET_RAW = 0.02
QUIET_BASE64 = 0.35
_raw_ok: set[str] = set()     # TVs whose shell passed the raw-bytes self-test (not re-run)


async def stream_h264(host: str, size: str = "640x360", bitrate: int = 1_000_000,
                      port: int | None = None):
    """Yield raw H.264 (Annex B) chunks from the TV's screenrecord, restarting it every ~3 min.

    The first item is ("mode", "raw" | "base64", quiet_seconds). In raw mode the shell's pty is
    switched to pass bytes through unchanged; otherwise the video is piped through base64 on
    the TV (the pty rewrites newlines) and decoded here, ignoring whitespace.
    """
    if not re.fullmatch(r"\d{3,4}x\d{3,4}", size):
        raise ValueError("size must look like 1280x720")
    session = await connect_any(host, port)
    record = (f"screenrecord --time-limit 170 --size {size} --bit-rate {int(bitrate)} "
              "--output-format=h264 - 2>/data/local/tmp/gremote-sr.err")
    # The self-test writes 64 KB on the TV; once a TV has passed it, don't make it work again.
    raw = host in _raw_ok or await _raw_pty_ok(session)
    if raw:
        _raw_ok.add(host)
    command = ("rm -f /data/local/tmp/gremote-sr.err; "
               + (f"stty -opost; {record}" if raw else f"{record} | base64"))
    yield ("mode", "raw" if raw else "base64", QUIET_RAW if raw else QUIET_BASE64)
    failures = 0
    try:
        while failures < 3:
            got_data = False
            pending = ""
            try:
                if raw:
                    async for chunk in session._dev.streaming_shell(
                            command, transport_timeout_s=10.0, read_timeout_s=60.0, decode=False):
                        if chunk:
                            got_data = True
                            yield bytes(chunk)
                else:
                    async for chunk in session._dev.streaming_shell(
                            command, transport_timeout_s=10.0, read_timeout_s=60.0):
                        pending += "".join(chunk.split())
                        usable = len(pending) - len(pending) % 4
                        if usable:
                            got_data = True
                            yield base64.b64decode(pending[:usable])
                            pending = pending[usable:]
            except Exception as exc:    # read timeout, dropped link, ...
                if not got_data:
                    _raw_ok.discard(host)          # test the shell again next time
                    await _raise_encoder_error(session, size, exc)
                    failures += 1
                    if failures >= 3:
                        raise DeviceError(f"Screen stream failed: {type(exc).__name__}: {exc}") from exc
                    continue
            if not got_data:
                await _raise_encoder_error(session, size, None)
            failures = 0
    finally:
        await session.__aexit__()


async def screen_diagnostics(host: str, port: int | None = None) -> str:
    """What the TV says about a running screenrecord (second connection, read-only commands)."""
    session = await connect_any(host, port)
    lines = []
    try:
        for label, command in (
            ("screenrecord stderr", "cat /data/local/tmp/gremote-sr.err"),
            ("screenrecord processes", "(ps -A 2>/dev/null || ps) | grep -i screenrecord"),
            ("encoder-related log lines", "logcat -d -t 600 2>&1 | grep -i -E "
             "'screenrecord|MediaCodec|OMX|encoder' | tail -25"),
        ):
            try:
                out = await session.shell(command, timeout=15.0)
            except DeviceError as exc:
                out = f"(failed: {exc})"
            lines.append(f"-- {label}\n{out or '(empty)'}")
    finally:
        await session.__aexit__()
    return "\n".join(lines)


async def benchmark(host: str, port: int | None = None) -> dict:
    """Time each stage of screen capture so we know where the preview is slow."""
    import shutil

    session = await connect_any(host, port)
    steps: list[dict] = []

    async def step(name: str, command: str, timeout: float = 30.0, keep: int = 400):
        started = time.monotonic()
        try:
            out = await session.shell(command, timeout=timeout)
        except DeviceError as exc:
            out = f"FAILED: {exc}"
        steps.append({"step": name, "ms": int((time.monotonic() - started) * 1000),
                      "output": out[:keep]})

    try:
        await step("display size", "wm size; wm density")
        await step("cpu / android", "getprop ro.product.cpu.abi; getprop ro.build.version.sdk; "
                                    "getprop ro.product.model; nproc")
        await step("tools present", "for t in screencap screenrecord base64 gzip ffmpeg; do "
                                    "echo $t=$(command -v $t); done")
        await step("screencap options", "screencap -h 2>&1 | head -15", keep=900)
        await step("screenrecord options", "screenrecord --help 2>&1 | head -30", keep=1500)
        await step("round trip (tiny command)", "echo ok")
        await step("screencap raw, discarded on TV (capture cost)", "screencap > /dev/null")
        await step("screencap PNG, discarded on TV (capture + PNG encode)",
                   "screencap -p > /dev/null")
        await step("PNG size in bytes", "screencap -p | wc -c")
        await step("transport: 2 MB of base64 text over this ADB link",
                   "head -c 1500000 /dev/zero | base64 -w 0", keep=40)
        await step("screenrecord H.264 1280x720, 3 s, bytes produced",
                   "screenrecord --time-limit 3 --size 1280x720 --bit-rate 4000000 "
                   "--output-format=h264 - 2>/data/local/tmp/sr.err | wc -c; "
                   "head -c 300 /data/local/tmp/sr.err", timeout=40)
    finally:
        await session.__aexit__()
    server = {t: shutil.which(t) for t in ("adb", "ffmpeg")}
    return {"steps": steps, "server_tools": server, "adb_port": session.port}
