import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEED = os.path.join(REPO, "shared-mihomo", "speed", "vpngate-speed.py")


class SpeedSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("speed_sources", SPEED)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        with open(SPEED, encoding="utf-8") as f:
            cls.src = f.read()

    def test_no_keyed_or_scraped_sources(self):
        for needle in ("proxycheck", "scamalytics", "whoamibb", "hub-gpcfg"):
            self.assertNotIn(needle, self.src)

    def test_peer_url_defaults_empty(self):
        # Empty means proxy blocks are built locally from the shared template.
        self.assertEqual(self.mod.PEER_URL, "")

    def test_alive_roster_is_identity_source(self):
        self.assertIn("alive-nodes.json", self.src)
        self.assertTrue(hasattr(self.mod, "alive_nodes_file"))

    def test_local_block_uses_shared_template_cipher(self):
        ovpn = {"cipher": "AES-128-CBC", "auth": "SHA1", "mtu": 1100, "ping": 10,
                "ca": "CA", "cert": "CERT", "key": "KEY"}
        name, block, port = self.mod.proxy_block(
            {"ip": "1.1.1.1", "tcp_port": 443, "cc": "JP", "proto": "udp"}, ovpn, 34000)
        self.assertIn("cipher: AES-128-CBC", block)
        self.assertIn("auth: SHA1", block)
        self.assertIn("mtu: 1100", block)
        self.assertIn("server: 1.1.1.1", block)
        self.assertEqual(port, 34000)

    def test_legacy_score_scrubber_exists(self):
        self.assertTrue(hasattr(self.mod, "scrub_legacy_scores"))


if __name__ == "__main__":
    unittest.main()
