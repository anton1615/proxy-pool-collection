import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WindscribeHostTest(unittest.TestCase):
    def test_snapshot_exists_and_has_embedded_ui(self):
        path = os.path.join(REPO, "windscribe-host-webui", "wspool.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("UI_HTML", src)
        self.assertIn("const BASE", src)
        self.assertIn("def _ui_token", src)

    def test_runner_points_at_snapshot(self):
        path = os.path.join(REPO, "webui", "windscribe_host.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("windscribe-host-webui", src)
        self.assertIn("UI_TOKEN", src)
        self.assertIn("API_PORT", src)

    def test_runtime_state_is_gitignored(self):
        gi = os.path.join(REPO, "windscribe-host-webui", ".gitignore")
        with open(gi, encoding="utf-8") as f:
            text = f.read()
        for needed in ("state.json", "accounts/", "inbox/", "uitoken"):
            self.assertIn(needed, text)


if __name__ == "__main__":
    unittest.main()
