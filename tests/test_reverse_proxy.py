import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webui"))
import server  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/cross"):
            self.send_response(302)
            self.send_header("Location", "http://evil.example/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        EchoHandler.seen.append((self.path, dict(self.headers)))
        body = json.dumps({"path": self.path, "host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_echo():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class ReverseProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = start_echo()
        cls.echo_port = cls.echo.server_address[1]
        cls.cfg = {
            "listen": "127.0.0.1", "port": 0,
            "host_allowlist": ["127.0.0.1"],
            "pools": {"urn": {"label": "UR", "upstream": "http://127.0.0.1:%d" % cls.echo_port,
                              "ui_path": "urn-dashboard"},
                      "vg": {"label": "VG", "upstream": "http://127.0.0.1:%d" % cls.echo_port,
                             "controller": "http://127.0.0.1:1"}},
        }
        fd, cls.cfg_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cls.cfg, f)
        server.CONFIG_PATH = cls.cfg_path
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.echo.shutdown()
        os.unlink(cls.cfg_path)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_prefix_strip_and_query(self):
        code, _, body = self.get("/p/urn/ui-state?q=1")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["path"], "/ui-state?q=1")
        self.assertTrue(any(p[0] == "/ui-state?q=1" for p in EchoHandler.seen))

    def test_redirect_rewritten_under_prefix(self):
        # urllib follows redirects by default; use a non-following opener.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)
        try:
            r = op.open(self.url("/p/urn/redirect"), timeout=5)
            self.assertEqual(r.headers.get("Location"), "/p/urn/landed")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.headers.get("Location"), "/p/urn/landed")

    def test_cross_origin_location_dropped(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)
        try:
            r = op.open(self.url("/p/urn/cross"), timeout=5)
            self.assertIsNone(r.headers.get("Location"))
        except urllib.error.HTTPError as e:
            self.assertIsNone(e.headers.get("Location"))

    def test_ssrf_path_rejected(self):
        before = len(EchoHandler.seen)
        code, _, _ = self.get("/p/urn//evil.example/x")
        self.assertEqual(code, 400)
        self.assertEqual(len(EchoHandler.seen), before)

    def test_traversal_rejected(self):
        code, _, _ = self.get("/p/urn/../etc/passwd")
        self.assertEqual(code, 400)

    def test_shell_served(self):
        code, _, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Proxy Pool Collection", body)

    def test_vg_ui_state_shape(self):
        # controller unreachable -> alive is null, but the blob still returns.
        code, _, body = self.get("/p/vg/ui-state")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertIn("clients", d)
        self.assertIn("nodes", d)

    def test_unknown_pool_404(self):
        code, _, _ = self.get("/p/nope/x")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
