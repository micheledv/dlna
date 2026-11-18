"""Extended M3U playlists."""

from __future__ import annotations

from typing import Iterable

from dlna.contentdir import Item


def m3u(items: Iterable[Item]) -> str:
    lines = ["#EXTM3U"]
    for item in items:
        url = item.url
        if not url:
            continue
        seconds = item.duration_seconds
        length = round(seconds) if seconds is not None else -1
        title = " ".join(item.title.split())  # no newlines inside #EXTINF
        lines.append(f"#EXTINF:{length},{title}")
        lines.append(url)
    return "\n".join(lines) + "\n"
