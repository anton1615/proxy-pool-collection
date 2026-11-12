import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(REPO, "shared-mihomo", "api", "register.py")


class StubController(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fail = False

    def log_message(self, *a):
        pass

    def _respond(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if StubController.fail:
            return self._respond({"error": "down"}, 502)
        if self.path.startswith("/providers/proxies/hub"):
            return self._respond({"proxies": [
                {"name": "VG-JP-1.1.1.1", "alive": True},
                {"name": "VG-US-2.2.2.2", "alive": True},
                {"name": "VG-DE-3.3.3.3", "alive": False}]})
        if self.path.startswith("/connections"):
            return self._respond({"connections": []})
        return self._respond({})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._respond({})

    def do_DELETE(self):
        self._respond({})


class RegisterApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctrl = ThreadingHTTPServer(("127.0.0.1", 0), StubController)
        threading.Thread(target=cls.ctrl.serve_forever, daemon=True).start()
        ctrl_url = "http://127.0.0.1:%d" % cls.ctrl.server_address[1]
        cls.state = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        cls.state.close()
        os.environ["SHARED_MIHOMO_API"] = ctrl_url
        os.environ["SHARED_MIHOMO_REG_STATE"] = cls.state.name
        os.environ["SHARED_MIHOMO_REG_BIND"] = "127.0.0.1"
        os.environ["SHARED_MIHOMO_REG_PORT"] = "0"
        spec = importlib.util.spec_from_file_location("register_api", REG)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.ctrl.shutdown()
        os.unlink(cls.state.name)

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_full_lifecycle(self):
        code, reg = self.get("/register?uid=alpha")
        self.assertEqual(code, 200)
        self.assertIn(reg["port"], self.mod.PORTS)
        self.assertTrue(reg["node"].startswith("VG-"))
        code, status = self.get("/status")
        self.assertEqual(code, 200)
        self.assertIn("alpha", status["clients"])
        code, rot = self.get("/rotate?uid=alpha")
        self.assertEqual(code, 200)
        self.assertIn("killed_idle_conns", rot)
        self.assertEqual(rot["uid"], "alpha")
        code, nodes = self.get("/nodes")
        self.assertEqual(code, 200)
        self.assertIn("VG-JP-1.1.1.1", nodes["nodes"])
        code, dele = self.get("/delete?uid=alpha")
        self.assertEqual(code, 200)
        self.assertTrue(dele["deleted"])

    def test_unknown_uid_rotate_404(self):
        code, _ = self.get("/rotate?uid=ghost")
        self.assertEqual(code, 404)

    def test_missing_uid_400(self):
        code, _ = self.get("/rotate")
        self.assertEqual(code, 400)

    def test_safe_idle_grace_default(self):
        self.assertGreaterEqual(self.mod.KILL_RESP_GRACE, 15)

    def test_controller_failure_is_502(self):
        StubController.fail = True
        try:
            code, _ = self.get("/register?uid=beta")
            self.assertEqual(code, 502)
        finally:
            StubController.fail = False


if __name__ == "__main__":
    unittest.main()
