import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "shared-mihomo", "webui", "ui.html")


class VgUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.src = f.read()

    def test_script_syntax(self):
        js = re.search(r"<script>(.*?)</script>", self.src, re.S).group(1)
        p = "/tmp/ppc-vg-ui.js"
        with open(p, "w") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_one_rotate_button_no_quota_no_dropdown(self):
        self.assertIn("隨機切換", self.src)
        self.assertIn("api/rotate", self.src)
        self.assertNotIn("<select", self.src)
        self.assertNotIn("quota", self.src.lower())
        self.assertNotIn("country", self.src.lower())
        self.assertIn("ui-state", self.src)

    def test_relative_base_convention(self):
        self.assertIn("const BASE = location.pathname", self.src)
        self.assertIn("BASE + \"ui-state\"", self.src)


if __name__ == "__main__":
    unittest.main()
