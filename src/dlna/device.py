"""UPnP device description -> MediaServer."""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from urllib.parse import urljoin

from dlna import xmlutil

CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:"
USER_AGENT = "dlna-cli/0.1 UPnP/1.1"
HTTP_TIMEOUT = 10.0


@dataclass
class MediaServer:
    name: str
    udn: str
    location: str
    control_url: str
    service_type: str = CONTENT_DIRECTORY + "1"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MediaServer:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def fetch(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_description(data: bytes, location: str) -> MediaServer | None:
    """Find the ContentDirectory service in a device description, if any."""
    root = xmlutil.parse(data)
    base = xmlutil.text(root, "URLBase") or location
    # The service may live on an embedded device, so search every device.
    for dev in root.iter("device"):
        for svc in dev.iterfind("serviceList/service"):
            service_type = xmlutil.text(svc, "serviceType")
            if service_type.startswith(CONTENT_DIRECTORY):
                return MediaServer(
                    name=xmlutil.text(dev, "friendlyName") or location,
                    udn=xmlutil.text(dev, "UDN"),
                    location=location,
                    control_url=urljoin(base, xmlutil.text(svc, "controlURL")),
                    service_type=service_type,
                )
    return None


def describe(location: str, timeout: float = HTTP_TIMEOUT) -> MediaServer | None:
    try:
        return parse_description(fetch(location, timeout), location)
    except (OSError, ET.ParseError):
        return None
