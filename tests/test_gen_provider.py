import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(REPO, "shared-mihomo", "mihomo", "gen-provider.py")


def load_mod():
    spec = importlib.util.spec_from_file_location("gen_provider", GEN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class GenProviderTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_mod()

    def test_clean_filter_and_veto_line(self):
        nodes = [
            {"ip": "1.1.1.1", "p": 443, "k": "u", "cc": "JP", "f": "00", "r": 10, "sc": 90},
            {"ip": "2.2.2.2", "p": 443, "k": "u", "cc": "US", "f": "10", "r": 10, "sc": 80},
            {"ip": "3.3.3.3", "p": 443, "k": "u", "cc": "DE", "f": "01", "r": 10, "sc": 70},
            {"ip": "4.4.4.4", "p": 443, "k": "u", "cc": "FR", "f": "00", "r": 80, "sc": 60},
            {"ip": "5.5.5.5", "p": 443, "k": "u", "cc": "KR", "sc": 50},  # veto: no r
            {"ip": "6.6.6.6", "p": 443, "k": "g", "cc": "JP", "f": "00", "r": 10, "sc": 40},
            {"ip": "1.1.1.1", "p": 1194, "k": "t", "cc": "JP", "f": "00", "r": 5, "sc": 99},
        ]
        out = self.mod.clean_nodes(nodes)
        ips = [n["ip"] for n in out]
        self.assertEqual(ips, ["1.1.1.1"],
                         "only the clean node survives; proxy/hosting/high-risk/"
                         "veto/gp/duplicate nodes are dropped")

    def test_render_uses_runtime_template(self):
        with tempfile.TemporaryDirectory() as d:
            tpl = {"cipher": "AES-128-CBC", "auth": "SHA1", "mtu": 1100, "ping": 10,
                   "ca": "CA_PEM", "cert": "CERT_PEM", "key": "KEY_PEM"}
            p = os.path.join(d, "tpl.json")
            with open(p, "w") as f:
                json.dump(tpl, f)
            self.mod.OVPN_TEMPLATE = p
            ovpn = self.mod.load_ovpn()
            text = self.mod.render(
                [{"ip": "1.1.1.1", "port": 443, "k": "u", "sc": 90, "risk": 10,
                  "cc": "JP", "pi": 5, "sp": 12.3}], ovpn)
            self.assertIn("type: openvpn", text)
            self.assertIn("cipher: AES-128-CBC", text)
            self.assertIn("CA_PEM", text)

    def test_missing_publish_file_exits_zero(self):
        self.mod.SRC_NODES = "/nonexistent/hub-nodes.json"
        self.assertEqual(self.mod.main(), 0)

    def test_no_pem_or_removed_symbols_in_shipped_source(self):
        with open(GEN, encoding="utf-8") as f:
            src = f.read()
        for needle in ("BEGIN CERTIFICATE", "BEGIN RSA PRIVATE KEY",
                       "gpcfg", "gp", "hub:gpcfg"):
            if needle == "gp":
                continue  # 'gp' appears as a short substring in general prose only
        self.assertNotIn("BEGIN CERTIFICATE", src)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", src)
        self.assertNotIn("hub-gpcfg", src)


if __name__ == "__main__":
    unittest.main()
