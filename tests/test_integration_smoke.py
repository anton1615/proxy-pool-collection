"""End-to-end smoke: one URL, four tabs, VPNGate Rotate visible.

Starts the real webui/server.py with four stub upstreams (only each pool's
external API is stubbed; the dashboard HTML/JS is the shipped real file), then
drives it with Playwright at desktop and mobile widths.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "webui"))


class StubPool(BaseHTTPRequestHandler):
    """Serves a minimal dashboard-shaped HTML + API-shaped JSON for one pool."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # register.py-shaped responses for the VPNGate API upstream.
        if self.path.startswith("/status"):
            return self._json({"count": 2, "clients": {
                "alpha": {"port": 7911, "node": "VG-JP-1.1.1.1", "added": 1},
                "beta": {"port": 7912, "node": "VG-US-2.2.2.2", "added": 2}}})
        if self.path.startswith("/nodes"):
            return self._json({"count": 2, "nodes": ["VG-JP-1.1.1.1", "VG-US-2.2.2.2"]})
        if self.path.startswith("/ui-state"):
            return self._json({"instances": [], "host": "stub"})
        body = ("<!doctype html><title>stub pool</title>"
                "<div class='card'>stub dashboard</div>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_server(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class IntegrationSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pools = {k: free_server(StubPool) for k in ("foxy", "urn", "ws")}
        cls.reg = free_server(StubPool)  # /status, /nodes, /rotate
        base = {k: "http://127.0.0.1:%d" % s.server_address[1]
                for k, s in cls.pools.items()}
        cfg = {
            "listen": "127.0.0.1", "port": 0,
            "host_allowlist": ["127.0.0.1"],
            "pools": {
                "foxy": {"label": "FoxyVPN", "upstream": base["foxy"], "ui_path": "tok"},
                "urn": {"label": "URnetwork", "upstream": base["urn"], "ui_path": "tok"},
                "ws": {"label": "Windscribe", "upstream": base["ws"], "ui_path": "tok"},
                "vg": {"label": "VPNGate", "upstream": "http://127.0.0.1:%d" % cls.reg.server_address[1],
                       "controller": "http://127.0.0.1:1"},
            },
        }
        fd, cls.cfg_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.environ["PPC_CONFIG"] = cls.cfg_path
        # Launch the real server process on a fixed free port.
        import server
        server.CONFIG_PATH = cls.cfg_path
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for s in list(cls.pools.values()) + [cls.reg]:
            s.shutdown()
        os.unlink(cls.cfg_path)

    def test_syntax_and_sanitize(self):
        r = subprocess.run([sys.executable, "scripts/sanitize_scan.py"],
                           cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # py_compile every shipped .py
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
            for fn in files:
                if fn.endswith(".py"):
                    p = os.path.join(root, fn)
                    subprocess.run([sys.executable, "-m", "py_compile", p], check=True)

    def test_four_tabs_and_rotate_in_browser(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(self.url, wait_until="domcontentloaded")
            page.wait_for_selector("nav button")
            self.assertEqual(page.locator("nav button").count(), 4)
            labels = page.locator("nav button").all_text_contents()
            self.assertEqual([t.strip() for t in labels],
                             ["FoxyVPN", "URnetwork", "Windscribe", "VPNGate"])
            # Switch to the VPNGate tab and verify the real shipped UI renders.
            page.locator("nav button[data-pool='vg']").click()
            page.wait_for_timeout(400)
            frame = page.frame_locator("#frame")
            frame.locator(".card, .empty").first.wait_for(timeout=5000)
            self.assertIn("隨機切換", frame.locator("body").inner_text())
            self.assertEqual(frame.locator("select").count(), 0)
            self.assertNotIn("quota", frame.locator("body").inner_text().lower())
            # Other tabs still load.
            page.locator("nav button[data-pool='urn']").click()
            page.wait_for_timeout(300)
            self.assertIn("stub", frame.locator("body").inner_text().lower())
            # Mobile width: sidebar becomes a row, no horizontal overflow.
            page.set_viewport_size({"width": 380, "height": 720})
            page.wait_for_timeout(200)
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 380)
            page.screenshot(path="/tmp/ppc-four-tabs.png", full_page=True)
            self.assertEqual(errors, [], "no page JS errors")
            browser.close()


if __name__ == "__main__":
    unittest.main()
