"""SSDP network discovery, mirroring Home Assistant's braviatv integration.

HA discovers Sony BRAVIA TVs via SSDP using:

    st:           urn:schemas-sony-com:service:ScalarWebAPI:1
    manufacturer: Sony Corporation

It then reads friendlyName / modelName / UDN from the LOCATION device-description
XML. We do the same: send an M-SEARCH for that service type, collect unicast
replies, and enrich each with a best-effort fetch of its LOCATION XML.
"""

import asyncio
import re
import socket
import time
import urllib.request
import urllib.error

MCAST_GRP = "239.255.255.250"
MCAST_PORT = 1900
SONY_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"
SONY_MANUFACTURER = "Sony Corporation"

_HEADER_RE = re.compile(rb"([A-Za-z0-9-]+):\s*(.*?)\r\n")
_XML_TAGS = ("friendlyName", "manufacturer", "modelName", "modelNumber", "UDN")


def _parse_ssdp(data: bytes) -> dict | None:
    headers: dict[str, str] = {}
    for key, value in _HEADER_RE.findall(data):
        headers[key.decode(errors="replace").lower()] = value.decode(errors="replace").strip()
    if not headers.get("location"):
        return None
    return {
        "location": headers["location"],
        "st": headers.get("st", ""),
        "usn": headers.get("usn", ""),
        "server": headers.get("server", ""),
    }


def _scan(st: str, timeout: float) -> list[dict]:
    """Blocking SSDP M-SEARCH; returns raw responses keyed by LOCATION."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        # Also listen for devices that answer on the multicast group.
        mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.bind(("", 0))
        sock.settimeout(0.5)

        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {MCAST_GRP}:{MCAST_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {int(timeout)}\r\n"
            f"ST: {st}\r\n"
            "\r\n"
        ).encode()
        sock.sendto(message, (MCAST_GRP, MCAST_PORT))

        found: dict[str, dict] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = _parse_ssdp(data)
            if parsed:
                parsed["host"] = addr[0]
                found[parsed["location"]] = parsed
        return list(found.values())
    finally:
        sock.close()


def _fetch_xml(location: str) -> str:
    req = urllib.request.Request(location, headers={"User-Agent": "gremote/0.1"})
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def _enrich(location: str) -> dict:
    try:
        xml = await asyncio.to_thread(_fetch_xml, location)
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    out: dict = {}
    for tag in _XML_TAGS:
        m = re.search(r"<" + tag + r"[^>]*>\s*(.*?)\s*</" + tag + r">", xml, re.S | re.I)
        if m:
            out[tag] = m.group(1).strip()
    return out


async def discover_sony_tvs(timeout: float = 4.0) -> list[dict]:
    raw = await asyncio.to_thread(_scan, SONY_ST, timeout)
    results: list[dict] = []
    seen_hosts: set[str] = set()
    for item in raw:
        host = item["host"]
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        info = await _enrich(item["location"])
        results.append({
            "host": host,
            "friendly_name": info.get("friendlyName") or item["server"],
            "manufacturer": info.get("manufacturer"),
            "model": info.get("modelName"),
            "udn": info.get("UDN"),
            "st": item["st"],
            "location": item["location"],
        })
    return results
