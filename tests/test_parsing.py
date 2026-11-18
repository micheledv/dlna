import unittest
from pathlib import Path

from dlna import contentdir, device, playlist, ssdp

FIXTURES = Path(__file__).parent / "fixtures"


class DeviceTests(unittest.TestCase):
    def test_parse_description(self):
        data = (FIXTURES / "device.xml").read_bytes()
        server = device.parse_description(data, "http://10.0.0.5:8200/rootDesc.xml")
        self.assertEqual(server.name, "NAS: media")
        self.assertEqual(server.udn, "uuid:4d696e69-444c-164e-9d41-001122334455")
        self.assertEqual(server.control_url, "http://10.0.0.5:8200/ctl/ContentDir")
        self.assertEqual(server.service_type, "urn:schemas-upnp-org:service:ContentDirectory:1")

    def test_url_base_wins(self):
        data = (FIXTURES / "device.xml").read_text().replace(
            "<specVersion>", "<URLBase>http://10.0.0.9:9000/base/</URLBase><specVersion>"
        )
        server = device.parse_description(data.encode(), "http://10.0.0.5:8200/rootDesc.xml")
        self.assertEqual(server.control_url, "http://10.0.0.9:9000/ctl/ContentDir")

    def test_no_content_directory(self):
        data = b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>Printer</friendlyName></device></root>'
        self.assertIsNone(device.parse_description(data, "http://x/"))


class SsdpTests(unittest.TestCase):
    def test_parse_response(self):
        data = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLocation: http://10.0.0.5:8200/rootDesc.xml\r\nST: x\r\n\r\n"
        self.assertEqual(ssdp.parse_response(data)["location"], "http://10.0.0.5:8200/rootDesc.xml")


class DidlTests(unittest.TestCase):
    def setUp(self):
        self.entries = contentdir.parse_didl((FIXTURES / "didl_movies.xml").read_text())

    def test_entries(self):
        folder, live, home, song = self.entries
        self.assertIsInstance(folder, contentdir.Container)
        self.assertEqual((folder.id, folder.title, folder.child_count), ("64$1$0", "Sci-Fi", 2))
        self.assertEqual(live.title, "AC/DC Live & Loud")
        self.assertEqual([live.kind, home.kind, song.kind], ["video", "video", "audio"])

    def test_best_resource_skips_thumbnail_transcode_subtitles(self):
        live = self.entries[1]
        res = live.best_resource()
        self.assertEqual(res.url, "http://10.0.0.5:8200/MediaItems/1.mkv")
        self.assertEqual(res.size, 1048576000)
        self.assertEqual(res.resolution, "1920x1080")
        self.assertAlmostEqual(live.duration_seconds, 6125.5)

    def test_missing_duration(self):
        self.assertIsNone(self.entries[2].duration_seconds)

    def test_parse_duration(self):
        self.assertEqual(contentdir.parse_duration("0:00:10"), 10)
        self.assertEqual(contentdir.parse_duration("12:00:00.5"), 43200.5)
        self.assertEqual(contentdir.parse_duration("0:00:01.1/2"), 1.5)
        self.assertIsNone(contentdir.parse_duration("garbage"))
        self.assertIsNone(contentdir.parse_duration(None))


class PlaylistTests(unittest.TestCase):
    def test_m3u(self):
        items = [e for e in contentdir.parse_didl((FIXTURES / "didl_movies.xml").read_text())
                 if isinstance(e, contentdir.Item)]
        items.append(contentdir.Item(id="x", parent_id="y", title="no res"))
        self.assertEqual(
            playlist.m3u(items),
            "#EXTM3U\n"
            "#EXTINF:6126,AC/DC Live & Loud\nhttp://10.0.0.5:8200/MediaItems/1.mkv\n"
            "#EXTINF:-1,Home video\nhttp://10.0.0.5:8200/MediaItems/2.mp4\n"
            "#EXTINF:200,Theme song\nhttp://10.0.0.5:8200/MediaItems/3.mp3\n",
        )


if __name__ == "__main__":
    unittest.main()
