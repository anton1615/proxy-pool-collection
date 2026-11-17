import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "webui", "ray.html")


class RayUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.src = f.read()

    def test_script_syntax(self):
        js = re.search(r"<script>(.*?)</script>", self.src, re.S).group(1)
        p = "/tmp/ppc-ray-ui.js"
        with open(p, "w") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_read_only_two_endpoints(self):
        # Quota/port changes must stay in the ray-pool API, not this dashboard.
        self.assertIn('BASE + "api/status"', self.src)
        self.assertIn('BASE + "api/nodes"', self.src)
        for verb in ("register", "rotate", "node?", "delete", "get", "sub"):
            self.assertNotIn("api/" + verb, self.src)
        self.assertNotIn("<select", self.src)

    def test_relative_base_convention(self):
        self.assertIn("const BASE = location.pathname", self.src)
        self.assertIn("fetch(url, {cache: \"no-store\"})", self.src)

    def test_render_shows_quota_and_clients(self):
        for token in ("帳號", "活端點", "剩餘額度", "尚無客戶端", "fmtMb", "fmtWhen"):
            self.assertIn(token, self.src)


if __name__ == "__main__":
    unittest.main()
