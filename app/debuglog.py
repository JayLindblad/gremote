"""In-memory ring buffer of recent events, shown in the dashboard's Debug window.

Nothing here is persisted: it resets when the service restarts. Secrets (PSKs,
passwords) are redacted before they are stored.
"""

import itertools
import logging
import threading
import time
from collections import deque

log = logging.getLogger("gremote")

MAX_ENTRIES = 500
SENSITIVE = {"psk", "password", "pin", "token", "secret", "cookie"}

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)
_ids = itertools.count(1)
_lock = threading.Lock()


def redact(value):
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in SENSITIVE else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def record(message: str, *, level: str = "info", kind: str = "event",
           device: str | None = None, action: str | None = None,
           params: dict | None = None, duration_ms: int | None = None,
           detail: str | None = None) -> None:
    """level: info | warn | error."""
    entry = {
        "id": next(_ids),
        "ts": time.time(),
        "level": level,
        "kind": kind,
        "device": device,
        "action": action,
        "params": redact(params) if params else None,
        "duration_ms": duration_ms,
        "message": message,
        "detail": detail,
    }
    with _lock:
        _entries.append(entry)
    py_level = {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO)
    log.log(py_level, "%s%s%s", f"[{device}] " if device else "",
            f"{action}: " if action else "", message)


def entries(after: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _entries if e["id"] > after]


def clear() -> None:
    with _lock:
        _entries.clear()
