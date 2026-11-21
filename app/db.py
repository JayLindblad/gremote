"""Tiny SQLite-backed device registry.

Devices are stored as a flat table. Per-device credentials (e.g. the Sony
pre-shared key) live in the JSON "config" column so the schema stays stable as
new driver types are added.
"""

import json
import sqlite3
import time
import uuid

from .config import settings


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                host TEXT NOT NULL,
                mac TEXT,
                config TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["config"] = json.loads(d.get("config") or "{}")
    return d


def list_devices() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_device(device_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_device(device_id: str | None, *, name: str, type: str, host: str,
                  mac: str | None, config: dict | None) -> dict:
    device_id = device_id or uuid.uuid4().hex
    cfg = json.dumps(config or {})
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO devices (id, name, type, host, mac, config, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                host = excluded.host,
                mac = excluded.mac,
                config = excluded.config
            """,
            (device_id, name, type, host, mac, cfg, int(time.time())),
        )
    return get_device(device_id)


def delete_device(device_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    return cur.rowcount > 0
