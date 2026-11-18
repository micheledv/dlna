"""End-to-end tests against a fake ContentDirectory served by http.server."""

import io
import os
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from xml.sax.saxutils import escape

from dlna import DlnaError, cli, contentdir
from dlna.contentdir import Container, Item
from dlna.device import MediaServer
from dlna.resolve import Resolver, save_cache

DIDL_OPEN = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
)


def container(id, parent, title):
    return (f'<container id="{id}" parentID="{parent}"><dc:title>{escape(title)}</dc:title>'
            f"<upnp:class>object.container.storageFolder</upnp:class></container>")


def item(id, parent, title, kind="video", mime="video/mp4", url=None):
    return (f'<item id="{id}" parentID="{parent}"><dc:title>{escape(title)}</dc:title>'
            f"<upnp:class>object.item.{kind}Item</upnp:class>"
            f'<res duration="0:01:00" protocolInfo="http-get:*:{mime}:*">{url or f"http://media/{id}"}</res></item>')


# id -> children (DIDL snippets)
TREE = {
    "0": [container("1", "0", "Movies"), container("2", "0", "Audio"), container("3", "0", "Big"),
          container("4", "0", "Shows")],
    "1": [container("11", "1", "AC/DC"), item("12", "1", "Dune"), item("13", "1", "Alien")],
    "11": [item("111", "11", "Live"), item("112", "11", "Song", kind="audio", mime="audio/mpeg")],
    "2": [],
    "3": [item(f"3-{i}", "3", f"clip {i:03d}") for i in range(450)],
    # Two shows with a "Season 1" each, plus a virtual view repeating one file.
    "4": [container("41", "4", "Lost"), container("42", "4", "Dark"), container("43", "4", "All episodes")],
    "41": [container("411", "41", "Season 1")],
    "411": [item("4111", "411", "Pilot")],
    "42": [container("421", "42", "Season 1"), container("422", "42", "Season 2")],
    "421": [item("4211", "421", "Secrets")],
    "422": [item("4221", "422", "Lies")],
    # A virtual view exposing the same files (same URLs) under other ids.
    "43": [item("431", "43", "Pilot", url="http://media/4111"), container("432", "43", "Dark")],
    "432": [container("4321", "432", "Season 1"), container("4322", "432", "Season 2")],
    "4321": [item("43211", "4321", "Secrets", url="http://media/4211")],
    "4322": [item("43221", "4322", "Lies", url="http://media/4221")],
}
METADATA = {"11": container("11", "1", "AC/DC")}


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        field = lambda name: re.search(f"<{name}>(.*?)</{name}>", body).group(1)
        object_id, flag = field("ObjectID"), field("BrowseFlag")
        start, count = int(field("StartingIndex")), int(field("RequestedCount"))
        Handler.requests.append((self.headers["SOAPACTION"], object_id, flag, start))

        if flag == "BrowseMetadata":
            snippets = [METADATA[object_id]] if object_id in METADATA else None
        else:
            snippets = TREE.get(object_id)
        if snippets is None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"<s:Envelope xmlns:s='x'><s:Body><s:Fault><detail><UPnPError>"
                             b"<errorCode>701</errorCode><errorDescription>No such object</errorDescription>"
                             b"</UPnPError></detail></s:Fault></s:Body></s:Envelope>")
            return
        total = len(snippets)
        page = snippets[start:start + count] if count else snippets
        didl = DIDL_OPEN + "".join(page) + "</DIDL-Lite>"
        resp = (
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            f"<Result>{escape(didl)}</Result><NumberReturned>{len(page)}</NumberReturned>"
            f"<TotalMatches>{total}</TotalMatches><UpdateID>1</UpdateID>"
            "</u:BrowseResponse></s:Body></s:Envelope>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class SoapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        port = cls.httpd.server_address[1]
        cls.server = MediaServer(
            name="Fake NAS",
            udn="uuid:fake",
            location=f"http://127.0.0.1:{port}/desc.xml",
            control_url=f"http://127.0.0.1:{port}/ctl",
        )

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        Handler.requests.clear()
        self.cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache_dir.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache_dir.name})
        env.start()
        self.addCleanup(env.stop)
        save_cache([self.server])
        # Never hit the real network from tests.
        ssdp = mock.patch("dlna.ssdp.discover", return_value=[])
        ssdp.start()
        self.addCleanup(ssdp.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_soap_action_header(self):
        contentdir.children(self.server, "0")
        self.assertEqual(Handler.requests[0][0], '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"')

    def test_pagination(self):
        entries = contentdir.children(self.server, "3")
        self.assertEqual(len(entries), 450)
        self.assertEqual([r[3] for r in Handler.requests], [0, 200, 400])

    def resolve_one(self, path):
        server, matches = Resolver().resolve(path)
        self.assertEqual(len(matches), 1, [m.path for m in matches])
        return server, matches[0]

    def test_resolve_path(self):
        server, m = self.resolve_one(r"fake/movies/AC\/DC/live")
        self.assertEqual(server.udn, "uuid:fake")
        self.assertIsInstance(m.entry, Item)
        self.assertEqual(m.entry.url, "http://media/111")
        self.assertEqual(m.path, r"Fake NAS/Movies/AC\/DC/Live")

    def test_resolve_object_id(self):
        _, m = self.resolve_one("Fake NAS/#11/Song")
        self.assertEqual(m.entry.url, "http://media/112")

    def test_resolve_descendant(self):
        _, matches = Resolver().resolve("Fake NAS//season 1")
        self.assertEqual([m.path for m in matches], ["Fake NAS/Shows/Lost/Season 1", "Fake NAS/Shows/Dark/Season 1"])

    def test_resolve_descendant_dedupes_views(self):
        _, matches = Resolver().resolve("Fake NAS/Shows//pilot")
        self.assertEqual([m.path for m in matches], ["Fake NAS/Shows/Lost/Season 1/Pilot"])

    def test_resolve_descendant_prefix_fallback(self):
        # No title is exactly "dar": prefix matches, and the mirrored Dark is dropped.
        _, matches = Resolver().resolve("Fake NAS//dar")
        self.assertEqual([m.path for m in matches], ["Fake NAS/Shows/Dark"])
        _, matches = Resolver().resolve("Fake NAS//season")
        self.assertEqual([m.path for m in matches], ["Fake NAS/Shows/Lost/Season 1", "Fake NAS/Shows/Dark/Season 1",
                                                     "Fake NAS/Shows/Dark/Season 2"])

    def test_resolve_glob_then_plain(self):
        # "Season 2" exists only under Dark: missing under other matches is fine.
        _, matches = Resolver().resolve("Fake NAS/Shows/*/Season 2")
        self.assertEqual([m.path for m in matches], ["Fake NAS/Shows/Dark/Season 2"])

    def test_resolve_glob_nothing(self):
        with self.assertRaisesRegex(DlnaError, "nothing matches"):
            Resolver().resolve("Fake NAS/Movies/zz*")

    def test_resolve_errors(self):
        with self.assertRaisesRegex(DlnaError, "not a folder"):
            Resolver().resolve("Fake NAS/Movies/Dune/x")
        with self.assertRaisesRegex(DlnaError, "No such object"):
            Resolver().resolve("Fake NAS/#nope")

    def test_walk(self):
        titles = [i.title for i in contentdir.walk(self.server, "1")]
        self.assertEqual(titles, ["Live", "Song", "Dune", "Alien"])

    def test_cli_ls(self):
        code, out = self.run_cli("ls", "Fake NAS/Movies")
        self.assertEqual(code, 0)
        self.assertEqual(out, "AC\\/DC/\nDune\nAlien\n")

    def test_cli_ls_recursive(self):
        code, out = self.run_cli("ls", "-r", "fake/mov/")
        self.assertEqual(code, 0)
        self.assertEqual(out, "Fake NAS/Movies/AC\\/DC/\nFake NAS/Movies/AC\\/DC/Live\nFake NAS/Movies/AC\\/DC/Song\n"
                              "Fake NAS/Movies/Dune\nFake NAS/Movies/Alien\n")

    def test_cli_ls_dirs(self):
        self.assertEqual(self.run_cli("ls", "-d", "Fake NAS"), (0, "Movies/\nAudio/\nBig/\nShows/\n"))
        code, out = self.run_cli("ls", "-rd", "Fake NAS")
        self.assertEqual(out, "Fake NAS/Movies/\nFake NAS/Movies/AC\\/DC/\nFake NAS/Audio/\nFake NAS/Big/\n"
                              "Fake NAS/Shows/\nFake NAS/Shows/Lost/\nFake NAS/Shows/Lost/Season 1/\n"
                              "Fake NAS/Shows/Dark/\nFake NAS/Shows/Dark/Season 1/\nFake NAS/Shows/Dark/Season 2/\n"
                              "Fake NAS/Shows/All episodes/\nFake NAS/Shows/All episodes/Dark/\n"
                              "Fake NAS/Shows/All episodes/Dark/Season 1/\nFake NAS/Shows/All episodes/Dark/Season 2/\n")

    def test_cli_ls_files(self):
        self.assertEqual(self.run_cli("ls", "-f", "Fake NAS/Movies"), (0, "Dune\nAlien\n"))
        code, out = self.run_cli("ls", "-rf", "Fake NAS//season 1")
        self.assertEqual(out, "Fake NAS/Shows/Lost/Season 1/Pilot\nFake NAS/Shows/Dark/Season 1/Secrets\n")
        code, out = self.run_cli("ls", "-rf", "Fake NAS/Movies")
        self.assertEqual(out, "Fake NAS/Movies/AC\\/DC/Live\nFake NAS/Movies/AC\\/DC/Song\n"
                              "Fake NAS/Movies/Dune\nFake NAS/Movies/Alien\n")

    def test_cli_ls_files_and_dirs_exclusive(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.run_cli("ls", "-d", "-f", "Fake NAS")

    def test_cli_ls_depth(self):
        code, out = self.run_cli("ls", "--depth", "1", "Fake NAS/Movies")
        self.assertEqual(out, "Fake NAS/Movies/AC\\/DC/\nFake NAS/Movies/Dune\nFake NAS/Movies/Alien\n")

    def test_recursive_paths_are_resolvable(self):
        _, out = self.run_cli("ls", "-r", "Fake NAS/Movies")
        for line in out.splitlines():
            self.resolve_one(line)

    def test_cli_ls_multiple_matches(self):
        self.assertEqual(self.run_cli("ls", "Fake NAS//Season*"),
                         (0, "Fake NAS/Shows/Lost/Season 1/\nFake NAS/Shows/Dark/Season 1/\nFake NAS/Shows/Dark/Season 2/\n"))
        code, out = self.run_cli("ls", "-r", "Fake NAS//Season 1")
        self.assertEqual(out, "Fake NAS/Shows/Lost/Season 1/\nFake NAS/Shows/Lost/Season 1/Pilot\n"
                              "Fake NAS/Shows/Dark/Season 1/\nFake NAS/Shows/Dark/Season 1/Secrets\n")

    def test_cli_m3u_descendant(self):
        code, out = self.run_cli("m3u", "Fake NAS//season 1")
        self.assertEqual(out, "#EXTM3U\n#EXTINF:60,Pilot\nhttp://media/4111\n#EXTINF:60,Secrets\nhttp://media/4211\n")
        code, out = self.run_cli("url", "-r", "Fake NAS/Shows")
        self.assertEqual(out, "http://media/4111\nhttp://media/4211\nhttp://media/4221\n")

    def test_cli_ls_servers(self):
        self.assertEqual(self.run_cli("ls"), (0, "Fake NAS/\n"))

    def test_cli_m3u_recursive_filtered(self):
        code, out = self.run_cli("m3u", "-r", "-t", "video", "Fake NAS/Movies")
        self.assertEqual(code, 0)
        self.assertEqual(out, "#EXTM3U\n#EXTINF:60,Live\nhttp://media/111\n"
                              "#EXTINF:60,Dune\nhttp://media/12\n#EXTINF:60,Alien\nhttp://media/13\n")

    def test_cli_url_empty_folder(self):
        code, _ = self.run_cli("url", "Fake NAS/Audio")
        self.assertEqual(code, 1)

    def test_cli_play_passes_player_args(self):
        with mock.patch("subprocess.call", return_value=0) as call:
            code, _ = self.run_cli("play", "--player", "mpv", "Fake NAS/Movies/Dune", "--", "--fs")
        self.assertEqual(code, 0)
        cmd = call.call_args[0][0]
        self.assertTrue(cmd[0].endswith("mpv"))
        self.assertEqual(cmd[1:], ["--fs", "--force-media-title=Dune", "http://media/12"])

    def test_cli_play_paused(self):
        with mock.patch("subprocess.call", return_value=0) as call:
            self.run_cli("play", "-P", "--player", "mpv", "Fake NAS/Movies/Dune")
        self.assertIn("--pause", call.call_args[0][0])

    def test_cli_play_paused_other_player(self):
        err = io.StringIO()
        with mock.patch("subprocess.call", return_value=0) as call, mock.patch("sys.stderr", err):
            self.run_cli("play", "-P", "--player", "true", "Fake NAS/Movies/Dune")
        self.assertNotIn("--pause", call.call_args[0][0])
        self.assertIn("only supported for mpv", err.getvalue())

    def test_cli_play_folder_uses_playlist(self):
        seen = {}

        def fake_call(cmd):
            with open(cmd[-1]) as f:
                seen["playlist"] = f.read()
            return 0

        with mock.patch("subprocess.call", side_effect=fake_call):
            self.run_cli("play", "Fake NAS/Movies")
        self.assertIn("#EXTINF:60,Dune\nhttp://media/12\n", seen["playlist"])

    def test_stale_cache_rediscovers(self):
        stale = MediaServer(name="Fake NAS", udn="uuid:fake", location="x", control_url="http://127.0.0.1:9/ctl")
        save_cache([stale])
        with mock.patch("dlna.resolve.discover_servers", return_value=[self.server]) as discover:
            code, out = self.run_cli("ls", "Fake NAS")
        discover.assert_called_once()
        self.assertEqual((code, out), (0, "Movies/\nAudio/\nBig/\nShows/\n"))


if __name__ == "__main__":
    unittest.main()
