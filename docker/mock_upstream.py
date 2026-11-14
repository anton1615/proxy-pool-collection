#!/usr/bin/env python3
"""One stdlib HTTP mock standing in for all four pool upstreams in the demo stack.

The shell (webui/server.py) proxies the configured host:port verbatim, so the
container config points every pool and the VPNGate controller at this single
port. This process returns the JSON shapes the VPNGate dashboard aggregates and
a small HTML body for the three upstream UI_HTML tabs. No real state, no creds.

Usage: python3 docker/mock_upstream.py [port]   (default 8080)
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODES = ["vpn-a", "vpn-b", "vpn-c"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            return self._send(200, json.dumps({"count": 0, "clients": {}}),
                              "application/json")
        if path == "/nodes":
            return self._send(200, json.dumps({"count": len(NODES), "nodes": NODES}),
                              "application/json")
        if path == "/providers/proxies/hub":
            return self._send(200, json.dumps({"proxies": []}), "application/json")
        # Generic dashboard body for the three UI_HTML tabs and /p/vg/api/*.
        html = ("<!doctype html><meta charset=utf-8>"
                "<title>Proxy Pool demo</title>"
                "<p>Mock upstream — no real pool connected.</p>")
        return self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        return self.do_GET()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write("mock upstream on http://0.0.0.0:%d\n" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
