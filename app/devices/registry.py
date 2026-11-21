"""Maps a stored device row to its driver instance."""

from .base import DeviceDriver
from .sony_bravia import SonyBravia

DRIVERS: dict[str, type[DeviceDriver]] = {
    SonyBravia.type: SonyBravia,
}


def driver_for(device: dict) -> DeviceDriver:
    cls = DRIVERS.get(device["type"])
    if cls is None:
        raise ValueError(f"Unsupported device type: {device['type']}")
    config = device.get("config") or {}
    return cls(host=device["host"], mac=device.get("mac"), **config)


def known_types() -> list[dict]:
    return [
        {"type": cls.type, "label": getattr(cls, "label", cls.type)}
        for cls in DRIVERS.values()
    ]
