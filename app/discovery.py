"""SSDP network discovery, mirroring Home Assistant's braviatv integration.

HA discovers Sony BRAVIA TVs via SSDP using:

    st:           urn:schemas-sony-com:service:ScalarWebAPI:1
    manufacturer: Sony Corporation

It then reads friendlyName / modelName / UDN from the LOCATION device-description
XML. We do the same: send an M-SEARCH for that service type, collect unicast
replies, and enrich each with a best-effort fetch of its LOCATION XML.
"""

import asyncio
import ipaddress
import os
import re
import socket
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse

import aiohttp

from .config import settings

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


SWEEP_PAYLOAD = {"method": "getInterfaceInformation", "id": 1, "params": [], "version": "1.0"}
SWEEP_CONCURRENCY = 128
MAX_SWEEP_HOSTS = 1024   # a /22


def _local_ip() -> str | None:
    """The IP this machine uses to reach the LAN (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _saved_device_subnets() -> list[str]:
    """The /24 around every device already added (by IP)."""
    try:
        from . import db
        hosts = [d.get("host") for d in db.list_devices()]
    except Exception:
        return []
    out: list[str] = []
    for host in hosts:
        try:
            net = str(ipaddress.ip_network(f"{host}/24", strict=False))
        except ValueError:
            continue
        if net not in out:
            out.append(net)
    return out


def sweep_subnets() -> list[str]:
    """What the sweep probes: GREMOTE_SCAN_SUBNET (comma-separated CIDRs) or our own /24.

    Inside Docker (e.g. Docker Desktop on a Mac, bridge network) our own /24 is Docker's
    internal network, not the LAN, so the subnets of devices already added are swept too.
    """
    cidrs = [c.strip() for c in settings.scan_subnet.split(",") if c.strip()]
    if cidrs:
        return cidrs
    own = _local_ip()
    if own:
        cidrs.append(str(ipaddress.ip_network(f"{own}/24", strict=False)))
    if _in_docker():
        cidrs += [c for c in _saved_device_subnets() if c not in cidrs]
    return cidrs


def _sweep_hosts() -> list[str]:
    own = _local_ip()
    hosts: list[str] = []
    for cidr in sweep_subnets():
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        hosts.extend(str(h) for h in net.hosts() if str(h) != own and str(h) not in hosts)
    return hosts[:MAX_SWEEP_HOSTS]


# Connect timed out (as opposed to refused / no answer): worth one more try.
_CONNECT_TIMEOUT = getattr(aiohttp, "ConnectionTimeoutError", aiohttp.ServerTimeoutError)
RETRY = object()


async def _probe(session: aiohttp.ClientSession, sem: asyncio.Semaphore, host: str):
    """Sony's getInterfaceInformation is public (no PSK) and answers on port 80."""
    async with sem:
        try:
            async with session.post(f"http://{host}/sony/system", json=SWEEP_PAYLOAD,
                                    allow_redirects=False) as resp:
                if resp.status not in (200, 404):
                    return None   # e.g. an Aruba AP's 302 captive-portal page
                data = await resp.json(content_type=None)
        except _CONNECT_TIMEOUT:
            return RETRY
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        if _is_sony_error(data):
            # Sony's API server is there but not answering (TV in standby, or IP control
            # off): still a TV. Its Cast description (port 8008) carries the name.
            cast = await _cast_description(session, host)
            if cast.get("manufacturer", "").lower().startswith("sony") or not cast:
                return {"host": host, "model": cast.get("friendlyName"),
                        "product": cast.get("modelName"), "asleep": True}
            return None
    result = data.get("result") if isinstance(data, dict) else None
    info = result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None
    if not info or not (info.get("modelName") or info.get("productName")):
        return None
    return {"host": host, "model": info.get("modelName"), "product": info.get("productName")}


def _is_sony_error(data) -> bool:
    """Sony's ScalarWebAPI error shape: {"error": [code, "message"], ...}."""
    err = data.get("error") if isinstance(data, dict) else None
    return isinstance(err, list) and len(err) == 2 and isinstance(err[0], int)


async def _cast_description(session: aiohttp.ClientSession, host: str) -> dict:
    try:
        async with session.get(f"http://{host}:8008/ssdp/device-desc.xml") as resp:
            xml = await resp.text() if resp.status == 200 else ""
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return {}
    out: dict = {}
    for tag in _XML_TAGS:
        m = re.search(r"<" + tag + r"[^>]*>\s*(.*?)\s*</" + tag + r">", xml, re.S | re.I)
        if m:
            out[tag] = m.group(1).strip()
    return out


async def _sweep() -> list[dict]:
    hosts = _sweep_hosts()
    if not hosts:
        return []
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=2.0, connect=1.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        found = await asyncio.gather(*(_probe(session, sem, h) for h in hosts))
        if _in_docker():
            # Docker Desktop's NAT drops some of a burst of connections, the TV's included;
            # give those addresses a second, gentler pass.
            again = [h for h, f in zip(hosts, found) if f is RETRY]
            sem = asyncio.Semaphore(SWEEP_CONCURRENCY // 2)
            found += await asyncio.gather(*(_probe(session, sem, h) for h in again))
    return [f for f in found if f and f is not RETRY]


async def _ssdp(timeout: float) -> list[dict]:
    raw = await asyncio.to_thread(_scan, SONY_ST, timeout)
    results: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        # The datagram's source can be an access point answering on the TV's
        # behalf (Aruba AirGroup etc.); the LOCATION URL carries the TV's own IP.
        host = urlparse(item["location"]).hostname or item["host"]
        if host in seen:
            continue
        seen.add(host)
        info = await _enrich(item["location"])
        results.append({
            "host": host,
            "friendly_name": info.get("friendlyName") or item["server"],
            "manufacturer": info.get("manufacturer"),
            "model": info.get("modelName"),
            "udn": info.get("UDN"),
            "st": item["st"],
            "location": item["location"],
            "found_by": ["ssdp"],
        })
    return results


async def discover_sony_tvs(timeout: float = 4.0) -> list[dict]:
    """SSDP (multicast) plus a unicast subnet sweep, merged by IP.

    The sweep finds TVs when an access point filters multicast on Wi-Fi.
    """
    ssdp_results, sweep_results = await asyncio.gather(
        _ssdp(timeout), _sweep(), return_exceptions=True)
    merged: dict[str, dict] = {}
    if not isinstance(ssdp_results, BaseException):
        for d in ssdp_results:
            merged[d["host"]] = d
    if not isinstance(sweep_results, BaseException):
        for s in sweep_results:
            existing = merged.get(s["host"])
            if existing:
                existing["found_by"].append("sweep")
                existing["model"] = existing.get("model") or s["model"]
            else:
                merged[s["host"]] = {
                    "host": s["host"],
                    "friendly_name": s["model"] or s["product"] or "Sony TV",
                    "manufacturer": "Sony",
                    "model": s["model"],
                    "udn": None,
                    "st": "",
                    "location": None,
                    "found_by": ["sweep"],
                }
            if s.get("asleep"):
                merged[s["host"]]["note"] = ("Not answering yet: turn the TV on and check "
                                             "IP control is enabled")
    if isinstance(ssdp_results, BaseException) and isinstance(sweep_results, BaseException):
        raise ssdp_results
    return sorted(merged.values(), key=lambda d: tuple(int(p) if p.isdigit() else 0
                                                       for p in d["host"].split(".")))
