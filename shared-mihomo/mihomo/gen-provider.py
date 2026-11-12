#!/usr/bin/env python3
"""shared-mihomo provider generator (vpngate-hub ExecStartPost hook).

Reads the hub's local publish file, applies the clean filter, writes the mihomo
provider YAML atomically, and hot-reloads the external controller.

Data source (all local, refreshed by vpngate-hub.timer):
  hub-nodes.json  hub's published node list (verified-only set)

Filter = the worker's clean=1 rules: f[0]!="1" (non-proxy), f[1]!="1"
(non-hosting), risk<=50 (clean>=50), IP dedup. Nodes lacking an `r` field are
veto-only entries and are dropped by the `r is None` guard.

The OpenVPN CA/cert/key template is NOT embedded: it is loaded at runtime from
SHARED_MIHOMO_OVPN_TEMPLATE (gitignored). A placeholder copy ships as
ovpn-template.example.json. This script always exits 0 so a failure never marks
the hub's oneshot unit failed; the old provider file stays in use.
"""
import json
import os
import re
import sys
import tempfile
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SRC_NODES = os.environ.get("SHARED_MIHOMO_NODES",
                           "/var/lib/vpngate-hub/publish/hub-nodes.json")
OUT = os.environ.get("SHARED_MIHOMO_PROVIDER",
                     os.path.join(BASE, "providers", "hub-nodes.yaml"))
API = os.environ.get("SHARED_MIHOMO_API", "http://127.0.0.1:7910")
OVPN_TEMPLATE = os.environ.get(
    "SHARED_MIHOMO_OVPN_TEMPLATE", os.path.join(BASE, "ovpn-template.json"))


def warn(msg):
    print("[shared-mihomo] WARNING: %s" % msg, file=sys.stderr, flush=True)


def load_nodes():
    with open(SRC_NODES, encoding="utf-8") as fh:
        d = json.load(fh)
    return [n for n in (d.get("nodes") or []) if isinstance(n, dict)]


def load_ovpn():
    with open(OVPN_TEMPLATE, encoding="utf-8") as fh:
        return json.load(fh)


def clean_nodes(nodes):
    out = []
    for n in nodes:
        f = str(n.get("f") or "")
        r = n.get("r")
        if r is None or (len(f) >= 2 and (f[0] == "1" or f[1] == "1")):
            continue
        if not re.match(r"^[0-9.]+$", str(n.get("ip") or "")):
            continue
        if n.get("k") not in ("t", "u"):
            continue
        try:
            risk = int(r)
            sc = int(n.get("sc") or 0)
            port = int(n.get("p") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 < port <= 65535 or risk > 50:
            continue
        cc = re.sub(r"[^A-Z]", "", str(n.get("cc") or "UN").upper()) or "UN"
        out.append({"ip": n["ip"], "port": port, "k": n["k"], "sc": sc,
                    "risk": risk, "cc": cc, "pi": n.get("pi"),
                    "sp": n.get("sp")})
    out.sort(key=lambda x: (x["risk"], -x["sc"]))
    dedup, seen = [], set()
    for x in out:
        if x["ip"] in seen:
            continue
        seen.add(x["ip"])
        dedup.append(x)
    return dedup


def block(name, text):
    return ["    %s: |" % name] + ["      " + l for l in text.rstrip().splitlines()]


def render(nodes, ovpn):
    L = ["proxies:"]
    for n in nodes:
        name = "VG-%s-%s" % (n["cc"], n["ip"])
        note = "# %s sc=%s risk=%s ping=%s speed=%sMbps" % (
            {"t": "tcp", "u": "udp"}[n["k"]], n["sc"], n["risk"],
            n["pi"], n["sp"])
        L += ["  - name: %s" % name, "    type: openvpn",
              "    server: %s" % n["ip"], "    port: %d" % n["port"],
              "    proto: %s" % ("tcp" if n["k"] == "t" else "udp"),
              "    udp: true", "    " + note,
              "    cipher: %s" % ovpn["cipher"],
              "    auth: %s" % ovpn["auth"],
              "    mtu: %s" % ovpn.get("mtu", 1100),
              "    ping: %s" % ovpn.get("ping", 10)]
        L += block("ca", ovpn["ca"])
        L += block("cert", ovpn["cert"])
        L += block("key", ovpn["key"])
    return "\n".join(L) + "\n"


def atomic_write(text):
    d = os.path.dirname(OUT)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hub-nodes.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, OUT)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def hot_reload():
    try:
        req = urllib.request.Request(API + "/providers/proxies/hub",
                                     method="PUT", data=b"")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as exc:  # noqa: BLE001 - failure must not block the hook
        warn("hot reload failed (%s); mihomo keeps the previous provider" % exc)
        return False


def main():
    try:
        nodes = clean_nodes(load_nodes())
        if not nodes:
            warn("clean filter produced 0 nodes; keeping the previous provider")
            return 0
        atomic_write(render(nodes, load_ovpn()))
        print("[shared-mihomo] provider updated: %d nodes -> %s"
              % (len(nodes), OUT), flush=True)
    except (IOError, OSError, ValueError, KeyError) as exc:
        warn("provider generation failed (%s); keeping previous file" % exc)
        return 0
    hot_reload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
