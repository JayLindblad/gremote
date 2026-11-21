"""Base interface every device driver implements."""

from abc import ABC, abstractmethod


class DeviceError(Exception):
    """Raised when talking to a device fails; surfaced to the API as a 502."""


class DeviceDriver(ABC):
    type: str = "base"
    label: str = "Device"

    def __init__(self, *, host: str, mac: str | None = None, **config):
        self.host = host
        self.mac = mac
        self.config = config

    @abstractmethod
    async def status(self) -> dict:
        """Return a small dict describing current device state."""

    @abstractmethod
    async def action(self, name: str, params: dict | None = None) -> dict:
        """Perform a named action and return a small result dict."""

    async def fetch_mac(self) -> str | None:
        """Best-effort: ask the device (or the ARP table) for its MAC address."""
        return None
