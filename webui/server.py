#!/usr/bin/env python3
"""One-URL shell for the four proxy pools.

Serves the tab shell at /, reverse-proxies /p/<pool>/... to each configured
upstream, and serves the VPNGate dashboard statically at /p/vg/.

Security: upstream URLs are built by string concatenation (never urljoin), the
stripped path is validated against traversal/protocol-relative forms, only an
allowlisted set of request headers is forwarded, Host is replaced, and the
upstream host:port is fixed in config. Upstream 3xx is returned to the browser,
never followed server-side.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PPC_CONFIG", os.path.join(BASE, "config.json"))
VG_UI = os.path.join(os.path.dirname(BASE), "shared-mihomo", "webui", "ui.html")

FORWARD_REQ_HEADERS = {"Accept", "Accept-Language", "Content-Type", "Content-Length",
                       "Cache-Control", "If-None-Match", "If-Modified-Since"}
FORWARD_RESP_HEADERS = {"Content-Type", "Content-Length", "Cache-Control",
                        "ETag", "Last-Modified", "Expires"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)
_CONFIG_CACHE = {"key": None, "cfg": None}


def load_config():
    try:
        st = os.stat(CONFIG_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        if _CONFIG_CACHE["cfg"] is not None:
            return _CONFIG_CACHE["cfg"]
        raise
    if key != _CONFIG_CACHE["key"]:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            _CONFIG_CACHE["cfg"] = json.load(fh)
        _CONFIG_CACHE["key"] = key
    return _CONFIG_CACHE["cfg"]


def forward_headers(hdrs, pool):
    extra = {k: v for k, v in hdrs.items() if k in FORWARD_RESP_HEADERS}
    loc = hdrs.get("Location")
    if loc and loc.startswith("/") and not loc.startswith("//"):
        extra["Location"] = "/p/%s/%s" % (pool, loc.lstrip("/"))
    return extra


def host_allowed(cfg, handler):
    host = (handler.headers.get("Host") or "").split(":")[0]
    allowed = cfg.get("host_allowlist") or [cfg.get("listen", "127.0.0.1")]
    return host in allowed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PPC/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("ppc " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _shell(self):
        try:
            with open(os.path.join(BASE, "index.html"), encoding="utf-8") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        except OSError as exc:
            self._send(500, "shell unavailable: %s" % exc)

    def _vg_static(self, rest):
        # /p/vg/ and /p/vg/<static file> render the first-party dashboard.
        if rest and rest != "index.html":
            self._send(404, "not found")
            return
        try:
            with open(VG_UI, encoding="utf-8") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        except OSError:
            self._send(502, "VPNGate dashboard not built yet")

    def _vg_ui_state(self, cfg):
        """Aggregate register.py /status + /nodes + controller liveness."""
        api = cfg["pools"]["vg"]["upstream"].rstrip("/")
        ctrl = os.environ.get("SHARED_MIHOMO_API", "http://127.0.0.1:7910").rstrip("/")

        def get(url, timeout=5):
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")

        try:
            status = get(api + "/status")
            nodes = get(api + "/nodes")
            try:
                prov = get(ctrl + "/providers/proxies/hub", timeout=2)
                alive = {p.get("name"): bool(p.get("alive")) for p in prov.get("proxies", [])}
            except Exception:
                alive = {}
            clients = {}
            for uid, rec in (status.get("clients") or {}).items():
                node = rec.get("node")
                clients[uid] = dict(rec, alive=alive.get(node))
            self._send(200, json.dumps(
                {"clients": clients, "nodes": nodes.get("nodes", []), "host": api},
                indent=2), "application/json")
        except Exception as exc:
            self._send(502, json.dumps({"error": repr(exc)}), "application/json")
    def _proxy(self, cfg, pool, rest, query, method):
        entry = cfg["pools"].get(pool)
        if not entry:
            self._send(404, "unknown pool")
            return
        # Reject traversal / protocol-relative / backslash forms before any join.
        # rest arrives without the /p/<pool>/ prefix; a leading "//" here means
        # the original path was /p/<pool>//host/... (an SSRF attempt).
        raw = rest
        if (".." in raw.split("/") or raw.startswith("/") or "\\" in raw
                or raw.startswith("//")):
            self._send(400, "bad path")
            return
        base = entry["upstream"].rstrip("/")
        # Fixed scheme+host+port from config; only the encoded remainder is joined.
        encoded = urllib.parse.quote(raw, safe="/%")
        url = base + "/" + encoded + (("?" + query) if query else "")
        headers = {k: v for k, v in self.headers.items() if k in FORWARD_REQ_HEADERS}
        headers["Host"] = urllib.parse.urlsplit(base).netloc
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        try:
            with OPENER.open(req, timeout=30) as resp:
                self._send(resp.status, resp.read(),
                           extra=forward_headers(resp.headers, pool))
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read(),
                       extra=forward_headers(exc.headers, pool))
        except Exception as exc:
            self._send(502, "upstream unreachable: %s" % repr(exc))

    def _dispatch(self, method):
        cfg = load_config()
        if not host_allowed(cfg, self):
            self._send(403, "host not allowed")
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            return self._shell()
        if path == "/p/vg" or path.startswith("/p/vg/"):
            rest = path[len("/p/vg/"):]
            if rest.startswith("/"):
                return self._send(400, "bad path")
            if rest == "ui-state":
                return self._vg_ui_state(cfg)
            if rest == "" or rest == "index.html":
                return self._vg_static(rest)
            # /p/vg/api/*
            if rest.startswith("api/"):
                return self._proxy(cfg, "vg", rest[len("api/"):], parsed.query, method)
            return self._send(404, "not found")
        if path.startswith("/p/"):
            parts = path[len("/p/"):].split("/", 1)
            pool = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            return self._proxy(cfg, pool, rest, parsed.query, method)
        self._send(404, "not found")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main():
    cfg = load_config()
    listen = cfg.get("listen", "127.0.0.1")
    port = int(cfg.get("port", 8787))
    srv = ThreadingHTTPServer((listen, port), Handler)
    sys.stderr.write("ppc shell on http://%s:%d\n" % (listen, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
