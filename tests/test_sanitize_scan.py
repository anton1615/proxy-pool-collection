import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import sanitize_scan  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class SanitizeScanTest(unittest.TestCase):
    def test_flags_first_party_private_ip_and_token(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "webui"))
            with open(os.path.join(root, "webui", "bad.py"), "w") as f:
                f.write("HOST = '100.64.10.10'\nKEY = 'tskey-abc123'\n")
            hits = sanitize_scan.scan(root)
            labels = {h[2] for h in hits}
            self.assertIn("private-ip", labels)
            self.assertIn("token-prefix", labels)

    def test_flags_removed_source_symbol_in_vendored_tree(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "pools", "x"))
            with open(os.path.join(root, "pools", "x", "a.py"), "w") as f:
                f.write("def _aes_cbc_decrypt(blob): pass\n")
            hits = sanitize_scan.scan(root)
            self.assertTrue(any(h[2] == "removed-source-symbol" for h in hits))

    def test_allows_documented_upstream_literals(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "pools", "x"))
            with open(os.path.join(root, "pools", "x", "README.md"), "w") as f:
                f.write("curl http://100.100.100.101:8080/register\n")
                f.write("Tailscale hands out addresses in 100.64.0.0/10.\n")
                f.write("Environment=PUBLIC_HOST=pool-host.example.ts.net\n")
                f.write('{"seedphrase": "example words here"}\n')
            self.assertEqual(sanitize_scan.scan(root), [])

    def test_first_party_tree_is_clean(self):
        self.assertEqual(sanitize_scan.scan(REPO), [],
                         "first-party files must have zero scanner hits")

if __name__ == "__main__":
    unittest.main()
