#!/usr/bin/env python3
"""vpngate-speed -- speed/quality daemon for the official-only VPNGate hub.

Long-lived daemon (systemd Type=simple). Each cycle:
  1. read publish/alive-nodes.json (the full alive roster, including nodes
     not yet measured) as the node-identity source.
  2. build one mihomo config with one select group + one mixed listener per
     node, using the shared OpenVPN template loaded at runtime.
  3. measure each node sequentially through the Cloudflare speed endpoint and
     update sp_ewma + speed timestamp in the hub SQLite.
  4. refresh a lightweight quality profile from ip-api (no key) and the public
     X4BNet CIDR lists.

No keyed enrichment, no scraped scores, no third-party peer worker. The
SPEED_PEER_URL env is optional: when set, an operator may point it at their
own worker; otherwise proxy blocks are built locally. Pure standard library.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("SPEED_BASE", "/var/lib/vpngate-hub")
DB_PATH = os.environ.get("SPEED_DB", os.path.join(BASE, "hub.sqlite"))
WORK_DIR = os.environ.get("SPEED_WORK", os.path.join(BASE, "speedwork"))
ALIVE_NODES_PATH = os.environ.get("SPEED_ALIVE",
                                  os.path.join(BASE, "publish", "alive-nodes.json"))
X4_PATH = os.environ.get("SPEED_X4", os.path.join(BASE, "x4ranges.json"))
OVPN_TEMPLATE = os.environ.get("SHARED_MIHOMO_OVPN_TEMPLATE",
                               os.path.join(BASE, "ovpn-template.json"))
MIHOMO = os.environ.get("SPEED_MIHOMO", "/usr/local/bin/mihomo")
PEER_URL = os.environ.get("SPEED_PEER_URL", "").rstrip("/")
SPEED_URL = os.environ.get("SPEED_URL",
                           "https://speed.cloudflare.com/__down?bytes=8000000")
WARM_URL = os.environ.get("SPEED_WARM_URL",
                          "https://speed.cloudflare.com/__down?bytes=200000")
IP_API = os.environ.get("SPEED_IP_API", "http://ip-api.com/batch?fields=query,proxy,hosting,isp")
X4_VPN = os.environ.get("SPEED_X4_VPN",
                        "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/vpn/ipv4.txt")
X4_DC = os.environ.get("SPEED_X4_DC",
                       "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/datacenter/ipv4.txt")

BATCH = int(os.environ.get("SPEED_BATCH", "48"))
MIN_AGE_S = float(os.environ.get("SPEED_MIN_AGE", "3600"))
IDLE_SLEEP = int(os.environ.get("SPEED_IDLE_SLEEP", "120"))
LOAD_LIMIT = float(os.environ.get("SPEED_LOAD_LIMIT", "3.0"))
MEASURE_SECS = int(os.environ.get("SPEED_MEASURE_SECS", "6"))
NODE_MAX_SECS = float(os.environ.get("SPEED_NODE_MAX_SECS", "20"))
UA = "Mozilla/5.0 (compatible; vpngate-speed/1.0)"


def log(msg):
    print(datetime.now(timezone.utc).strftime("[%H:%M:%S]") + " " + msg, flush=True)


def http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, timeout=30, payload=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def load_env_ovpn():
    """Load the shared OpenVPN template (gitignored in the repo)."""
    with open(OVPN_TEMPLATE, encoding="utf-8") as fh:
        return json.load(fh)


def alive_nodes_file():
    """The daemon's ONLY source of node identity: every alive node, no speed filter."""
    with open(ALIVE_NODES_PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    return [n for n in (d.get("nodes") or []) if isinstance(n, dict) and n.get("ip")]


def load_x4():
    """Public VPN/datacenter CIDR lists, refreshed at most daily."""
    try:
        st = os.stat(X4_PATH)
        if time.time() - st.st_mtime < 86400:
            with open(X4_PATH, encoding="utf-8") as fh:
                return json.load(fh)
    except OSError:
        pass
    out = {"vpn": [], "dc": []}
    for key, url in (("vpn", X4_VPN), ("dc", X4_DC)):
        try:
            out[key] = [l.strip() for l in http_text(url, 90).splitlines()
                        if l.strip() and not l.startswith("#")]
        except Exception as exc:
            log("x4 %s refresh failed (%s); keeping cached" % (key, exc))
    try:
        os.makedirs(os.path.dirname(X4_PATH), exist_ok=True)
        with open(X4_PATH, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    return out


def in_ranges(ip, ranges):
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in ranges:
            try:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
    except ValueError:
        return False
    return False


def assess_quality(rows, x4):
    """No-key quality profile from ip-api + public CIDR lists."""
    try:
        info = http_json(IP_API, 30, [{"query": r["ip"]} for r in rows[:100]])
    except Exception as exc:
        log("ip-api failed (%s); using CIDR-only profile" % exc)
        info = []
    by_ip = {x.get("query"): x for x in info if isinstance(x, dict)}
    out = {}
    for r in rows:
        ip = r["ip"]
        d = by_ip.get(ip, {})
        proxy = bool(d.get("proxy")) or in_ranges(ip, x4.get("vpn", []))
        hosting = bool(d.get("hosting")) or in_ranges(ip, x4.get("dc", []))
        out[ip] = {"proxy": proxy, "hosting": hosting,
                   "isp": d.get("isp", ""), "ts": int(time.time())}
    return out


def proxy_block(node, ovpn, port):
    """Build one mihomo openvpn proxy block locally from the shared template."""
    name = "SP-%s-%s" % (node.get("cc", "UN"), node["ip"])
    L = ['  - name: "%s"' % name, "    type: openvpn",
         "    server: %s" % node["ip"],
         "    port: %d" % int(node.get("tcp_port") or node.get("port") or 443),
         "    proto: %s" % node.get("proto", "udp"),
         "    udp: true",
         "    cipher: %s" % ovpn["cipher"],
         "    auth: %s" % ovpn["auth"],
         "    mtu: %s" % ovpn.get("mtu", 1100),
         "    ping: %s" % ovpn.get("ping", 10)]
    for key in ("ca", "cert", "key"):
        L += ["    %s: |" % key] + ["      " + l for l in ovpn[key].rstrip().splitlines()]
    return name, "\n".join(L), port


def fetch_peer_block(ip):
    """Optional operator-supplied peer worker; unused when SPEED_PEER_URL is empty."""
    if not PEER_URL:
        return None
    try:
        d = http_json(PEER_URL + "?ip=" + urllib.parse.quote(ip), 30)
        return d.get("yaml") if d.get("yaml") else None
    except Exception as exc:
        log("peer lookup failed for %s (%s)" % (ip, exc))
        return None


def build_cfg(blocks):
    proxies = "".join("\n" + b.strip("\n") for _n, b, _p in blocks) + "\n"
    groups, listeners, rules = [], [], []
    for name, _b, port in blocks:
        g = "G-%s" % name
        groups.append('  - name: "%s"\n    type: select\n    proxies: ["%s"]\n' % (g, name))
        listeners.append('  - name: "L-%s"\n    type: mixed\n    port: %d\n'
                         '    proxy: "%s"\n    udp: true\n' % (name, port, g))
        rules.append("  - SRC-PORT,%d,%s\n" % (port, g))
    return ("mode: rule\nlog-level: warning\nipv6: false\n"
            "external-controller: 127.0.0.1:0\n"
            "proxies:\n" + proxies + "proxy-groups:\n" + "".join(groups)
            + "listeners:\n" + "".join(listeners) + "rules:\n"
            + "".join(rules) + "  - MATCH,DIRECT\n")


def measure(node, port):
    """One curl process: warm-up + 3 measured downloads, median body time."""
    proxy = "http://127.0.0.1:%d" % port
    curl = ["curl", "-s", "-o", "/dev/null", "-w", "%{time_total} %{time_starttransfer}",
            "--max-time", str(MEASURE_SECS), "-x", proxy, SPEED_URL]
    warm = ["curl", "-s", "-o", "/dev/null", "--max-time", "3", "-x", proxy, WARM_URL]
    try:
        subprocess.run(warm, capture_output=True, timeout=5)
    except Exception:
        pass
    samples = []
    for _ in range(3):
        try:
            r = subprocess.run(curl, capture_output=True, text=True, timeout=NODE_MAX_SECS)
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                total, ttfb = float(parts[0]), float(parts[1])
                if total > ttfb > 0:
                    samples.append(8.0 / (total - ttfb))
        except Exception:
            continue
    if not samples:
        return None
    samples.sort()
    return round(samples[len(samples) // 2], 2)


def open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS quality (
        ip TEXT PRIMARY KEY, sp_ewma REAL DEFAULT 0, sp_ts REAL DEFAULT 0,
        sp_fail INTEGER DEFAULT 0, q TEXT, legacy_score INTEGER)""")
    return conn


def scrub_legacy_scores(conn):
    """Purge any legacy scraped score column left by an older daemon version."""
    conn.execute("UPDATE quality SET legacy_score=NULL WHERE legacy_score IS NOT NULL")
    conn.commit()


def due_nodes(conn, nodes):
    out = []
    for n in nodes:
        row = conn.execute("SELECT sp_ts, sp_fail FROM quality WHERE ip=?", (n["ip"],)).fetchone()
        if row is None or (time.time() - (row[0] or 0)) >= MIN_AGE_S:
            out.append(n)
    return out[:BATCH]


def record(conn, ip, mbps, quality):
    row = conn.execute("SELECT sp_ewma, sp_fail FROM quality WHERE ip=?", (ip,)).fetchone()
    prev = (row[0] if row else 0) or 0
    fail = (row[1] if row else 0) or 0
    if mbps is None:
        conn.execute("INSERT INTO quality(ip,sp_ts,sp_fail) VALUES(?,?,?) "
                     "ON CONFLICT(ip) DO UPDATE SET sp_ts=excluded.sp_ts, sp_fail=?",
                     (ip, time.time(), fail + 1, fail + 1))
    else:
        ewma = mbps if prev == 0 else 0.3 * mbps + 0.7 * prev
        conn.execute("INSERT INTO quality(ip,sp_ewma,sp_ts,sp_fail,q) VALUES(?,?,?,0,?) "
                     "ON CONFLICT(ip) DO UPDATE SET sp_ewma=excluded.sp_ewma, "
                     "sp_ts=excluded.sp_ts, sp_fail=0, q=excluded.q",
                     (ip, ewma, time.time(), json.dumps(quality.get(ip, {}))))
    conn.commit()


def loadavg_ok():
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0]) < LOAD_LIMIT
    except OSError:
        return True


def run_cycle():
    conn = open_db()
    scrub_legacy_scores(conn)
    if not loadavg_ok():
        log("load guard: skipping cycle")
        return
    nodes = due_nodes(conn, alive_nodes_file())
    if not nodes:
        log("no nodes due")
        return
    ovpn = load_env_ovpn()
    x4 = load_x4()
    quality = assess_quality(nodes, x4)
    blocks = []
    for i, n in enumerate(nodes):
        peer = fetch_peer_block(n["ip"])
        if peer:
            blocks.append(("SP-peer-%s" % n["ip"], peer, 34000 + i))
        else:
            blocks.append(proxy_block(n, ovpn, 34000 + i))
    cfg = os.path.join(WORK_DIR, "speed.yaml")
    os.makedirs(WORK_DIR, exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(build_cfg(blocks))
    proc = subprocess.Popen([MIHOMO, "-f", cfg, "-d", WORK_DIR],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        for (name, _b, port), n in zip(blocks, nodes):
            mbps = measure(n, port)
            record(conn, n["ip"], mbps, quality)
            log("%s %s -> %s Mbps" % (name, n["ip"], mbps))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main():
    log("start db=%s mihomo=%s batch=%d peer=%s"
        % (DB_PATH, MIHOMO, BATCH, PEER_URL or "(local blocks)"))
    while True:
        try:
            run_cycle()
        except Exception as exc:
            log("cycle error: %r" % exc)
        time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
