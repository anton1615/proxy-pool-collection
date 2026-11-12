import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "shared-mihomo", "hub", "vpngate-hub.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class HubSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("hub_sources", HUB)
        with open(HUB, encoding="utf-8") as f:
            cls.src = f.read()

    def test_official_sources_only(self):
        self.assertEqual(self.mod.SOURCE_ORDER,
                         ["official_csv", "official_client", "inbox"])

    def test_no_removed_source_symbols(self):
        for needle in ("giamping", "GIAMPING_", "_aes_cbc_decrypt",
                       "_giamping_message", "parse_gp_record", "parse_gp_config",
                       "adsid", "serversse", "serversgp", "hub-gpcfg",
                       "push_kv", "CF_API_BASE", "CF_ENV_PATH", "cryptography"):
            self.assertNotIn(needle, self.src)

    def test_keeps_official_fetchers_and_local_inbox(self):
        for name in ("fetch_official_csv", "fetch_official_client",
                     "fetch_inbox", "_rc4"):
            self.assertIn(name, self.src)

    def test_publish_writes_both_roster_and_verified_set(self):
        self.assertIn("alive-nodes.json", self.src)
        self.assertIn("hub-nodes.json", self.src)
        self.assertIn("require_speed=False", self.src)


if __name__ == "__main__":
    unittest.main()
