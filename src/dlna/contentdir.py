"""ContentDirectory Browse over SOAP, and DIDL-Lite parsing."""

from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, Union
from xml.sax.saxutils import escape

from dlna import DlnaError, xmlutil
from dlna.device import HTTP_TIMEOUT, USER_AGENT, MediaServer

PAGE_SIZE = 200

ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Browse xmlns:u="{service_type}">
<ObjectID>{object_id}</ObjectID>
<BrowseFlag>{flag}</BrowseFlag>
<Filter>*</Filter>
<StartingIndex>{start}</StartingIndex>
<RequestedCount>{count}</RequestedCount>
<SortCriteria></SortCriteria>
</u:Browse>
</s:Body>
</s:Envelope>"""


def parse_duration(value: str | None) -> float | None:
    """Parse a DIDL duration ("H+:MM:SS[.F+]") into seconds."""
    if not value:
        return None
    try:
        h, m, s = value.split(":")
        if "/" in s:  # "SS.F0/F1" fraction form
            whole, _, frac = s.partition(".")
            num, den = frac.split("/")
            seconds = int(whole) + int(num) / int(den)
        else:
            seconds = float(s)
        return int(h) * 3600 + int(m) * 60 + seconds
    except ValueError:
        return None


@dataclass
class Resource:
    url: str
    protocol_info: str = ""
    duration: str | None = None
    size: int | None = None
    resolution: str | None = None

    def _info(self, index: int) -> str:
        parts = self.protocol_info.split(":", 3)
        return parts[index] if len(parts) > index else ""

    @property
    def protocol(self) -> str:
        return self._info(0)

    @property
    def mime(self) -> str:
        return self._info(2)


@dataclass
class Container:
    id: str
    parent_id: str
    title: str
    child_count: int | None = None


@dataclass
class Item:
    id: str
    parent_id: str
    title: str
    upnp_class: str = ""
    resources: list[Resource] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """"video", "audio", "image" or "other", from the upnp:class."""
        for kind in ("video", "audio", "image"):
            if self.upnp_class.startswith(f"object.item.{kind}Item"):
                return kind
        return "other"

    def best_resource(self) -> Resource | None:
        """The resource most likely to be the original media file.

        Servers list thumbnails, subtitles and transcoded variants next to the
        original; prefer plain http-get of the item's own media type, not
        converted (DLNA.ORG_CI=1), not a thumbnail profile. Ties keep server order.
        """

        def score(r: Resource) -> int:
            s = 0
            if r.protocol != "http-get":
                s += 100
            if self.kind != "other" and not r.mime.startswith(self.kind):
                s += 10
            if "_TN" in r.protocol_info:
                s += 5
            if "DLNA.ORG_CI=1" in r.protocol_info:
                s += 1
            return s

        candidates = [r for r in self.resources if r.url]
        return min(candidates, key=score) if candidates else None

    @property
    def url(self) -> str | None:
        res = self.best_resource()
        return res.url if res else None

    @property
    def duration_seconds(self) -> float | None:
        res = self.best_resource()
        return parse_duration(res.duration) if res else None


Entry = Union[Container, Item]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_didl(didl: str) -> list[Entry]:
    root = xmlutil.parse(didl)
    entries: list[Entry] = []
    for el in root:
        title = xmlutil.text(el, "title")
        if el.tag == "container":
            entries.append(
                Container(
                    id=el.get("id", ""),
                    parent_id=el.get("parentID", ""),
                    title=title,
                    child_count=_int(el.get("childCount")),
                )
            )
        elif el.tag == "item":
            resources = [
                Resource(
                    url=(res.text or "").strip(),
                    protocol_info=res.get("protocolInfo", ""),
                    duration=res.get("duration"),
                    size=_int(res.get("size")),
                    resolution=res.get("resolution"),
                )
                for res in el.iterfind("res")
            ]
            entries.append(
                Item(
                    id=el.get("id", ""),
                    parent_id=el.get("parentID", ""),
                    title=title,
                    upnp_class=xmlutil.text(el, "class"),
                    resources=resources,
                )
            )
    return entries


def _soap_fault(body: bytes) -> str:
    try:
        root = xmlutil.parse(body)
    except ET.ParseError:
        return ""
    code = xmlutil.text(root, ".//errorCode")
    desc = xmlutil.text(root, ".//errorDescription")
    return f"UPnP error {code}: {desc}".strip(": ") if code or desc else ""


def browse(
    server: MediaServer,
    object_id: str,
    flag: str = "BrowseDirectChildren",
    start: int = 0,
    count: int = PAGE_SIZE,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[list[Entry], int, int]:
    """One Browse call. Returns (entries, NumberReturned, TotalMatches)."""
    body = ENVELOPE.format(
        service_type=server.service_type,
        object_id=escape(object_id),
        flag=flag,
        start=start,
        count=count,
    ).encode()
    req = urllib.request.Request(
        server.control_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{server.service_type}#Browse"',
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        with e:
            fault = _soap_fault(e.read())
        raise DlnaError(f"{server.name}: Browse({object_id!r}) failed: {fault or e}") from None

    try:
        response = xmlutil.parse(data).find(".//BrowseResponse")
        if response is None:
            raise DlnaError(f"{server.name}: malformed Browse response")
        entries = parse_didl(xmlutil.text(response, "Result") or "<DIDL-Lite/>")
    except ET.ParseError as e:
        raise DlnaError(f"{server.name}: unparseable Browse response: {e}") from None
    returned = _int(xmlutil.text(response, "NumberReturned")) or len(entries)
    total = _int(xmlutil.text(response, "TotalMatches")) or 0
    return entries, returned, total


def metadata(server: MediaServer, object_id: str) -> Entry:
    entries, _, _ = browse(server, object_id, flag="BrowseMetadata", count=0)
    if not entries:
        raise DlnaError(f"{server.name}: no object with id {object_id!r}")
    return entries[0]


def children(server: MediaServer, object_id: str) -> list[Entry]:
    """All children of a container, following pagination."""
    entries: list[Entry] = []
    start = 0
    while True:
        page, returned, total = browse(server, object_id, start=start)
        entries.extend(page)
        start += returned
        # TotalMatches=0 means "unknown" on some servers; stop on a short page.
        if returned == 0 or (total and start >= total) or (not total and returned < PAGE_SIZE):
            break
    return entries


def tree(
    server: MediaServer,
    object_id: str,
    max_depth: int | None = None,
    dirs_only: bool = False,
) -> Iterator[tuple[tuple[str, ...], Entry]]:
    """Every entry below a container, depth first, in server order.

    Yields (titles, entry) where titles is the path relative to object_id.
    max_depth=1 is just the children. Containers already visited are skipped,
    guarding against servers whose virtual views loop back on themselves.
    """
    visited = {object_id}

    def recurse(oid: str, prefix: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Entry]]:
        for entry in children(server, oid):
            if dirs_only and not isinstance(entry, Container):
                continue
            path = prefix + (entry.title,)
            yield path, entry
            if isinstance(entry, Container) and entry.id not in visited:
                visited.add(entry.id)
                if max_depth is None or len(path) < max_depth:
                    yield from recurse(entry.id, path)

    yield from recurse(object_id, ())


def walk(server: MediaServer, object_id: str) -> Iterator[Item]:
    """Every item below a container, depth first, in server order."""
    for _, entry in tree(server, object_id):
        if isinstance(entry, Item):
            yield entry
