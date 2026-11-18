# dlna

Browse DLNA/UPnP media servers from the terminal, play items in mpv (or any player), and export M3U playlists for players that can't browse DLNA themselves. Standard library only.

```sh
# install (or prefix commands with: uv run)
uv tool install -e .

# find servers (cached in ~/.cache/dlna/servers.json)
dlna discover

# list servers, then browse; -l adds kind, duration, size, resolution
dlna ls
dlna ls -l "NAS/Media/Movies"

# whole subtree as full, pasteable paths
# -d: folders only, -f: files only, --depth N: stop after N levels
dlna ls -r "NAS/Media"

# play one item, or a whole tree shuffled; args after -- go to mpv
# -P: start paused on the first item
dlna play "nas/med/mov/Dune"
dlna play -r -s "NAS/Audio/Jazz" -- --no-video
dlna play -r -P "NAS/Media/Shows"

# write a playlist (video only), or just print stream URLs
dlna m3u -r -t video "NAS/Media/Shows" -o shows.m3u
dlna url "NAS/Media/Movies/Dune"
```

Paths:
- Plain steps match exactly, case-insensitively, or by unique prefix: `nas/med/mov`.
- `//` matches at any depth below: `NAS//dune` finds `Dune (2021)`. Exact titles (ignoring case) win;
  if there are none, titles starting with the name match.
- `*`, `?`, `[...]` are case-insensitive globs: `NAS/Movies/dune*`, `NAS//*2019*`.
- Globs and `//` can match many entries: `play`/`m3u`/`url` use them all and `ls` lists them as full
  paths. The same file or folder exposed under several of the server's views is listed once.
- `//` walks the tree, so start it as deep as you can on big libraries.
- Escape `/ * ? [ \` in titles with a backslash (`AC\/DC`). A `#ID` step addresses a raw object id (`NAS/#64$1$2`).

Options: `--timeout` (SSDP wait), `--iface IP` (interface to discover on), `--refresh` (ignore the cache), `--player CMD` or `$DLNA_PLAYER` (default `mpv`).

Tests: `uv run python -m unittest discover -s tests`
