"""Server lookup (with a discovery cache) and "Server/Folder/Item" path resolution."""

from __future__ import annotations

import fnmatch
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from dlna import DlnaError, contentdir, device, ssdp
from dlna.contentdir import Container, Entry, Item
from dlna.device import MediaServer

T = TypeVar("T")

def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "dlna" / "servers.json"


def load_cache() -> list[MediaServer] | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        return [MediaServer.from_dict(d) for d in data["servers"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(servers: Sequence[MediaServer]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"servers": [s.to_dict() for s in servers]}, indent=2), encoding="utf-8")
    except OSError:
        pass  # the cache is only an optimisation


def discover_servers(timeout: float = 3.0, iface: str | None = None) -> list[MediaServer]:
    locations = ssdp.discover(timeout=timeout, iface=iface)
    with ThreadPoolExecutor(max_workers=8) as pool:
        described = list(pool.map(device.describe, locations))
    servers: dict[str, MediaServer] = {}
    for server in described:
        if server and server.udn not in servers:
            servers[server.udn] = server
    return sorted(servers.values(), key=lambda s: s.name.casefold())


GLOB_CHARS = "*?["


@dataclass
class Segment:
    """One step of a path."""

    text: str  # the literal title, escapes removed
    pattern: str | None = None  # fnmatch pattern if the step has unescaped wildcards
    is_id: bool = False  # "#<id>": a raw object id
    descendant: bool = False  # preceded by "//": match at any depth

    def matches(self, title: str) -> bool:
        """Set-style match used for globs and "//": case-insensitive, no prefixes."""
        if self.pattern is not None:
            return fnmatch.fnmatchcase(title.casefold(), self.pattern.casefold())
        return title.casefold() == self.text.casefold()


def split_path(path: str) -> list[Segment]:
    """Parse "Server/Folder//Name*/..." into segments.

    - "/" separates steps; "//" makes the next step match at any depth below.
    - "*", "?" and "[...]" are glob wildcards.
    - Backslash escapes the next character ("\\/", "\\*", "\\\\", "\\#").
    - An unescaped "#" at the start of a step marks a raw object id.
    Leading and trailing slashes are ignored.
    """
    segments: list[Segment] = []
    text: list[str] = []
    pattern: list[str] = []
    state = {"glob": False, "id": False, "escaped_start": False, "descendant": False}
    i = 0

    def flush():
        if text:
            literal = "".join(text)
            segments.append(
                Segment(
                    text=literal[1:] if state["id"] else literal,
                    pattern="".join(pattern) if state["glob"] else None,
                    is_id=state["id"],
                    descendant=state["descendant"] and bool(segments),
                )
            )
            state["descendant"] = False
        elif segments:
            state["descendant"] = True  # an empty step: "a//b"
        text.clear()
        pattern.clear()
        state.update(glob=False, id=False, escaped_start=False)

    while i < len(path):
        c = path[i]
        if c == "\\" and i + 1 < len(path):
            nxt = path[i + 1]
            if not text:
                state["escaped_start"] = True
            text.append(nxt)
            pattern.append(f"[{nxt}]" if nxt in GLOB_CHARS else nxt)
            i += 2
            continue
        if c == "/":
            flush()
        else:
            if not text and c == "#" and not state["escaped_start"]:
                state["id"] = True
            if c in GLOB_CHARS:
                state["glob"] = True
            text.append(c)
            pattern.append(c)
        i += 1
    flush()
    return segments


def escape_title(title: str) -> str:
    """Inverse of split_path for a single title."""
    escaped = "".join("\\" + c if c in "\\/" + GLOB_CHARS else c for c in title)
    return "\\" + escaped if escaped.startswith("#") else escaped


@dataclass
class Match:
    """A resolved entry with its title path from the server root (server name first)."""

    titles: tuple[str, ...]
    entry: Entry

    @property
    def path(self) -> str:
        return "/".join(escape_title(t) for t in self.titles)

    def key(self) -> tuple[str, str]:
        """Identity for de-duplication: servers expose one file under several views."""
        if isinstance(self.entry, Item):
            return ("item", self.entry.url or self.entry.id)
        return ("container", self.entry.id)


def pick(candidates: Sequence[T], name: str, key: Callable[[T], str], what: str = "entry") -> T:
    """Exact match, then case-insensitive match, then unique case-insensitive prefix."""
    for c in candidates:
        if key(c) == name:
            return c
    folded = name.casefold()
    for c in candidates:
        if key(c).casefold() == folded:
            return c
    prefixed = [c for c in candidates if key(c).casefold().startswith(folded)]
    if len(prefixed) == 1:
        return prefixed[0]
    if prefixed:
        names = ", ".join(repr(key(c)) for c in prefixed[:10])
        raise DlnaError(f"ambiguous {what} {name!r}: {names}")
    raise NotFound(f"no {what} matching {name!r}")


class NotFound(DlnaError):
    pass


class Resolver:
    """Finds servers (cache first, SSDP when needed) and resolves paths on them."""

    def __init__(self, timeout: float = 3.0, iface: str | None = None, refresh: bool = False):
        self.timeout = timeout
        self.iface = iface
        self._servers = None if refresh else load_cache()
        self.fresh = False  # True once servers come from a discovery in this run

    def servers(self) -> list[MediaServer]:
        if not self._servers:
            self.rediscover()
        return self._servers

    def rediscover(self) -> list[MediaServer]:
        self._servers = discover_servers(self.timeout, self.iface)
        self.fresh = True
        save_cache(self._servers)
        return self._servers

    def server(self, name: str) -> MediaServer:
        try:
            return self._match_server(name)
        except NotFound:
            if self.fresh:
                raise
            self.rediscover()
            return self._match_server(name)

    def _match_server(self, name: str) -> MediaServer:
        servers = self.servers()
        if not servers:
            raise NotFound("no DLNA media servers found (try --timeout or --iface)")
        try:
            return pick(servers, name, key=lambda s: s.name, what="server")
        except NotFound:
            for s in servers:
                if name.casefold() in s.udn.casefold():
                    return s
            raise

    def resolve(self, path: str) -> tuple[MediaServer, list[Match]]:
        """All entries a path matches, in server order, without duplicates.

        Plain steps must match exactly one child (exact, case-insensitive, or
        unique prefix); globs and "//" steps may match many.
        """
        segments = split_path(path)
        if not segments:
            raise DlnaError("empty path")
        server = self.server(segments[0].text)
        current = [Match((server.name,), Container(id="0", parent_id="-1", title=server.name))]
        for seg in segments[1:]:
            single = len(current) == 1
            found: list[Match] = []
            for m in current:
                if seg.is_id:
                    entry = contentdir.metadata(server, seg.text)
                    found.append(Match(m.titles + (entry.title,), entry))
                    continue
                if not isinstance(m.entry, Container):
                    if single:
                        raise DlnaError(f"{m.entry.title!r} is not a folder")
                    continue
                if seg.descendant:
                    below = [Match(m.titles + titles, e) for titles, e in contentdir.tree(server, m.entry.id)]
                    found += _match_all(below, seg)
                    continue
                kids = contentdir.children(server, m.entry.id)
                if seg.pattern is not None:
                    found += [Match(m.titles + (e.title,), e) for e in kids if seg.matches(e.title)]
                    continue
                try:
                    e = pick(kids, seg.text, key=lambda e: e.title)
                except NotFound:
                    if single:
                        raise
                    continue
                found.append(Match(m.titles + (e.title,), e))
            current = _drop_mirrors(server, _dedupe(found))
            if not current:
                raise NotFound(f"nothing matches {path!r}")
        return server, current


def _drop_mirrors(server: MediaServer, matches: list[Match]) -> list[Match]:
    """Drop folders that mirror an earlier match under another view.

    Servers often expose one folder under several views, with different ids.
    Only folders sharing a title are compared, by their children: item URLs
    and subfolder titles.
    """
    by_title: dict[str, list[Match]] = {}
    for m in matches:
        if isinstance(m.entry, Container):
            by_title.setdefault(m.entry.title, []).append(m)
    dropped = set()
    for group in by_title.values():
        if len(group) < 2:
            continue
        seen = set()
        for m in group:
            kids = contentdir.children(server, m.entry.id)
            signature = frozenset(
                ("item", e.url or e.id) if isinstance(e, Item) else ("container", e.title) for e in kids
            )
            if signature in seen:
                dropped.add(m.key())
            seen.add(signature)
    return [m for m in matches if m.key() not in dropped]


def _match_all(candidates: list[Match], seg: Segment) -> list[Match]:
    """Set-style matching for "//" steps.

    Globs match as patterns. A plain name follows the same precedence as
    pick(): exact (case-insensitive) matches if there are any, else every
    title starting with the name.
    """
    exact = [m for m in candidates if seg.matches(m.entry.title)]
    if exact or seg.pattern is not None:
        return exact
    folded = seg.text.casefold()
    return [m for m in candidates if m.entry.title.casefold().startswith(folded)]


def _dedupe(matches: list[Match]) -> list[Match]:
    seen = set()
    unique = []
    for m in matches:
        if m.key() not in seen:
            seen.add(m.key())
            unique.append(m)
    return unique
