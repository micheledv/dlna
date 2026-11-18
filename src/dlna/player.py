"""Launching an external player (mpv by default)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from dlna import DlnaError
from dlna.contentdir import Item
from dlna.playlist import m3u


def player_command(player: str | None = None) -> list[str]:
    cmd = shlex.split(player or os.environ.get("DLNA_PLAYER") or "mpv")
    if not cmd or not shutil.which(cmd[0]):
        raise DlnaError(f"player not found: {cmd[0] if cmd else player!r}")
    return cmd


def play(
    items: Sequence[Item],
    player: str | None = None,
    extra_args: Sequence[str] = (),
    paused: bool = False,
) -> int:
    """Run the player in the foreground; returns its exit code."""
    items = [i for i in items if i.url]
    if not items:
        raise DlnaError("nothing playable")
    cmd = player_command(player) + list(extra_args)
    is_mpv = Path(cmd[0]).name == "mpv"
    if paused:
        if is_mpv:
            cmd.append("--pause")
        else:
            print(f"dlna: --paused is only supported for mpv, ignoring it for {cmd[0]}", file=sys.stderr)

    if len(items) == 1:
        item = items[0]
        if is_mpv:
            cmd.append(f"--force-media-title={item.title}")
        return subprocess.call(cmd + [item.url])

    # Multiple items: a temporary playlist keeps titles and avoids huge argv.
    fd, path = tempfile.mkstemp(prefix="dlna-", suffix=".m3u")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(m3u(items))
        return subprocess.call(cmd + [path])
    finally:
        os.unlink(path)
