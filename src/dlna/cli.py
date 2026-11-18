"""Command line interface."""

from __future__ import annotations

import argparse
import itertools
import json
import random
import socket
import sys
import urllib.error
from typing import Iterable, Iterator, Sequence

from dlna import DlnaError, __version__, contentdir, player, playlist
from dlna.contentdir import Container, Entry, Item
from dlna.device import MediaServer
from dlna.resolve import Match, Resolver, escape_title

KINDS = ("video", "audio", "image")


# --- formatting -------------------------------------------------------------


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def _fmt_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return ""


def _print_table(rows: Sequence[Sequence[str]]) -> None:
    """Left-align all columns but the last, which is printed as is."""
    if not rows:
        return
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        cells = [c.ljust(w) for c, w in zip(r, widths)]
        print("  ".join(cells + [r[-1]]))


def _display_name(entry: Entry) -> str:
    return escape_title(entry.title) + ("/" if isinstance(entry, Container) else "")


def _entry_json(entry: Entry) -> dict:
    if isinstance(entry, Container):
        return {"type": "container", "id": entry.id, "title": entry.title, "child_count": entry.child_count}
    res = entry.best_resource()
    return {
        "type": "item",
        "id": entry.id,
        "title": entry.title,
        "kind": entry.kind,
        "class": entry.upnp_class,
        "url": res.url if res else None,
        "mime": res.mime if res else None,
        "duration": entry.duration_seconds,
        "size": res.size if res else None,
        "resolution": res.resolution if res else None,
    }


# --- helpers ----------------------------------------------------------------


def _is_network_error(e: OSError) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return False
    return isinstance(e, (urllib.error.URLError, socket.timeout, ConnectionRefusedError, ConnectionResetError))


def _items(server: MediaServer, matches: Sequence[Match], recursive: bool, kind: str | None) -> list[Item]:
    items: list[Item] = []
    seen = set()
    for m in matches:
        if isinstance(m.entry, Item):
            found = [m.entry]
        elif recursive:
            found = contentdir.walk(server, m.entry.id)
        else:
            found = [e for e in contentdir.children(server, m.entry.id) if isinstance(e, Item)]
        for item in found:
            # The same file often appears under several of the server's views.
            if (not kind or item.kind == kind) and (item.url or item.id) not in seen:
                seen.add(item.url or item.id)
                items.append(item)
    if not items:
        folders = any(isinstance(m.entry, Container) for m in matches)
        hint = " (use -r to include subfolders)" if folders and not recursive else ""
        where = repr(matches[0].entry.title) if len(matches) == 1 else f"{len(matches)} matches"
        raise DlnaError(f"no {kind or 'media'} items in {where}{hint}")
    return items


def _display_path(m: Match) -> str:
    return m.path + ("/" if isinstance(m.entry, Container) else "")


# --- commands ---------------------------------------------------------------


def cmd_discover(args, resolver: Resolver) -> int:
    servers = resolver.rediscover()
    if args.json:
        print(json.dumps([s.to_dict() for s in servers], indent=2))
    elif not servers:
        print("no DLNA media servers found", file=sys.stderr)
        return 1
    else:
        _print_table([(s.name, s.location) for s in servers])
    return 0


def cmd_ls(args, resolver: Resolver) -> int:
    if not args.path:
        servers = resolver.servers()
        if args.json:
            print(json.dumps([s.to_dict() for s in servers], indent=2))
        elif args.long:
            _print_table([(s.udn, s.location, escape_title(s.name) + "/") for s in servers])
        else:
            for s in servers:
                print(escape_title(s.name) + "/")
        return 0

    server, matches = resolver.resolve(args.path)
    recursive = args.recursive or args.depth is not None

    def wanted(e: Entry) -> bool:
        return isinstance(e, Container) if args.dirs else not (args.files and isinstance(e, Container))

    def listing() -> Iterator[tuple[str, Entry]]:
        # A single plain match lists its contents by bare name, like ls.
        if len(matches) == 1 and not recursive:
            entry = matches[0].entry
            for e in contentdir.children(server, entry.id) if isinstance(entry, Container) else [entry]:
                if wanted(e):
                    yield _display_name(e), e
            return
        # Otherwise full paths from the server root, pasteable back into play/m3u.
        seen = set()
        for m in matches:
            subtree: Iterable[Match] = ()
            if recursive and isinstance(m.entry, Container):
                subtree = (
                    Match(m.titles + titles, e)
                    for titles, e in contentdir.tree(server, m.entry.id, max_depth=args.depth, dirs_only=args.dirs)
                )
            # A single recursive match lists what is below it, not itself.
            head = [m] if len(matches) > 1 else []
            for sub in itertools.chain(head, subtree):
                if wanted(sub.entry) and sub.key() not in seen:
                    seen.add(sub.key())
                    yield _display_path(sub), sub.entry

    if args.json:
        rows = [dict(_entry_json(e), path=name) for name, e in listing()]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif args.long:
        rows = []
        for name, e in listing():
            if isinstance(e, Container):
                count = "" if e.child_count is None else str(e.child_count)
                rows.append(("dir", count, "", "", name))
            else:
                res = e.best_resource()
                rows.append(
                    (
                        e.kind,
                        _fmt_duration(e.duration_seconds),
                        _fmt_size(res.size if res else None),
                        (res.resolution or "") if res else "",
                        name,
                    )
                )
        _print_table(rows)
    else:
        for name, _ in listing():  # streamed: big trees start printing right away
            print(name, flush=True)
    return 0


def cmd_url(args, resolver: Resolver) -> int:
    server, matches = resolver.resolve(args.path)
    if args.all:
        if len(matches) != 1 or not isinstance(matches[0].entry, Item):
            raise DlnaError("--all needs a path to a single item")
        _print_table([(r.protocol_info, r.url) for r in matches[0].entry.resources])
        return 0
    for item in _items(server, matches, args.recursive, args.type):
        if item.url:
            print(item.url)
    return 0


def cmd_play(args, resolver: Resolver) -> int:
    server, matches = resolver.resolve(args.path)
    items = _items(server, matches, args.recursive, args.type)
    if args.shuffle:
        random.shuffle(items)
    return player.play(items, player=args.player, extra_args=args.extra, paused=args.paused)


def cmd_m3u(args, resolver: Resolver) -> int:
    server, matches = resolver.resolve(args.path)
    items = _items(server, matches, args.recursive, args.type)
    text = playlist.m3u(items)
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(items)} entries to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Global options are accepted before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="SSDP discovery wait in seconds (default 3)")
    common.add_argument("--iface", default=argparse.SUPPRESS, metavar="IP", help="local IP of the interface to discover on")
    common.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS, help="ignore the server cache")
    common.add_argument("--player", default=argparse.SUPPRESS, help="player command (default $DLNA_PLAYER or mpv)")

    parser = argparse.ArgumentParser(
        prog="dlna",
        parents=[common],
        description="Browse DLNA media servers, play items in mpv, export M3U playlists.",
        epilog=(
            "PATH is 'Server/Folder/.../Item'. Names match exactly, case-insensitively, or by unique prefix. "
            "'//' matches at any depth below ('NAS//dune' finds 'Dune (2021)'); '*', '?', '[...]' are case-insensitive globs; "
            "both may match many entries. Escape '/', '*', '?', '[' in titles with a backslash. "
            "A segment '#ID' addresses a raw object id. "
            "Arguments after '--' are passed to the player."
        ),
    )
    parser.set_defaults(timeout=3.0, iface=None, refresh=False, player=None)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("discover", parents=[common], help="find media servers on the network")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("ls", parents=[common], help="list servers, or the contents of a folder")
    p.add_argument("path", nargs="?", metavar="PATH")
    p.add_argument("-l", "--long", action="store_true", help="show kind, duration, size, resolution")
    p.add_argument("-r", "--recursive", action="store_true", help="list the whole subtree, as full paths")
    only = p.add_mutually_exclusive_group()
    only.add_argument("-d", "--dirs", action="store_true", help="only folders")
    only.add_argument("-f", "--files", action="store_true", help="only files (media items)")
    p.add_argument("--depth", type=int, metavar="N", help="recurse at most N levels (implies -r)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ls)

    def media_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("path", metavar="PATH")
        p.add_argument("-r", "--recursive", action="store_true", help="include items in subfolders")
        p.add_argument("-t", "--type", choices=KINDS, help="only items of this kind")

    p = sub.add_parser("url", parents=[common], help="print stream URL(s)")
    media_args(p)
    p.add_argument("--all", action="store_true", help="all resources of an item, with protocolInfo")
    p.set_defaults(func=cmd_url)

    p = sub.add_parser("play", parents=[common], help="play an item or folder in the player")
    media_args(p)
    p.add_argument("-s", "--shuffle", action="store_true")
    p.add_argument("-P", "--paused", action="store_true", help="start paused on the first item (mpv)")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("m3u", parents=[common], help="write an M3U playlist")
    media_args(p)
    p.add_argument("-o", "--output", metavar="FILE", help="output file (default stdout)")
    p.set_defaults(func=cmd_m3u)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]

    parser = build_parser()
    args = parser.parse_args(argv)
    if extra and args.command != "play":
        parser.error("arguments after '--' are only accepted by 'play'")
    args.extra = extra

    resolver = Resolver(timeout=args.timeout, iface=args.iface, refresh=args.refresh)
    try:
        try:
            return args.func(args, resolver)
        except OSError as e:
            # A cached server may have moved (new IP/port): rediscover once and retry.
            if not _is_network_error(e) or resolver.fresh:
                raise
            resolver.rediscover()
            return args.func(args, resolver)
    except DlnaError as e:
        print(f"dlna: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1  # e.g. piped into head
    except OSError as e:
        reason = getattr(e, "reason", None) or e
        print(f"dlna: {'network error: ' if _is_network_error(e) else ''}{reason}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
