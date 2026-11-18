"""SSDP (UPnP multicast) discovery."""

from __future__ import annotations

import select
import socket
import time

SSDP_ADDR = ("239.255.255.250", 1900)
MEDIA_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"


def _msearch(st: str, mx: int) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n"
        "USER-AGENT: dlna-cli/0.1 UPnP/1.1\r\n"
        "\r\n"
    ).encode()


def parse_response(data: bytes) -> dict[str, str]:
    """Parse an SSDP response into lower-cased header names -> values."""
    headers = {}
    for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def discover(timeout: float = 3.0, iface: str | None = None, st: str = MEDIA_SERVER) -> list[str]:
    """Return the unique description URLs (LOCATION) of devices answering an M-SEARCH."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if iface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        sock.bind((iface or "", 0))

        msg = _msearch(st, mx=max(1, min(5, int(timeout))))
        # UDP is lossy; a second packet is customary.
        for _ in range(2):
            sock.sendto(msg, SSDP_ADDR)

        locations: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                break
            data, _ = sock.recvfrom(65507)
            location = parse_response(data).get("location")
            if location and location not in locations:
                locations.append(location)
        return locations
    finally:
        sock.close()
