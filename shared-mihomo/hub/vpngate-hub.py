#!/usr/bin/env python3
"""vpngate-hub -- aggregate only the official public VPNGate node lists.

Sources (fetched independently; one failure never aborts a round):
  official_csv : official CSV endpoint (+ base64 .ovpn per node)
  official_client : official client data-file endpoint (obfuscated payload)
  inbox : local manual drops, one host per line

The merged set is probed for liveness, ranked, and written to local publish
JSON files. The hub deliberately does NOT push to any remote KV and does NOT
consume third-party/decompiled sources. shared-mihomo reads the local publish
files; vpngate-speed reads the alive-nodes roster.

Run once (systemd oneshot): vpngate-hub.py --once
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("HUB_BASE", "/var/lib/vpngate-hub")
PUBLISH_DIR = os.environ.get("HUB_PUBLISH_DIR", os.path.join(BASE, "publish"))
INBOX_DIR = os.environ.get("HUB_INBOX_DIR", os.path.join(BASE, "inbox"))
DB_PATH = os.environ.get("HUB_DB", os.path.join(BASE, "hub.sqlite"))
STATE_PATH = os.environ.get("HUB_STATE", os.path.join(BASE, "nodes.json"))
CSV_URL = os.environ.get("HUB_CSV_URL", "https://www.vpngate.net/api/iphone/")
CLIENT_URL = os.environ.get("HUB_CLIENT_URL", "http://client.api.vpngate2.jp/api/")
PROBE_TIMEOUT = float(os.environ.get("HUB_PROBE_TIMEOUT", "3"))
PROBE_CONCURRENCY = int(os.environ.get("HUB_PROBE_CONCURRENCY", "64"))
PUBLISH_MAX = int(os.environ.get("HUB_PUBLISH_MAX", "4000"))

SOURCE_ORDER = ["official_csv", "official_client", "inbox"]
PRIORITY = {"official_csv": 30, "official_client": 45, "inbox": 10}
DATA_FIELDS = ("ip", "tcp_port", "udp_port", "country", "country_long", "city",
               "operator", "sessions", "total_users", "uptime_days", "speed_mbps", "score")
DEFAULTS = {"ip": "", "tcp_port": 0, "udp_port": 0, "country": "", "country_long": "",
            "city": "", "operator": "", "sessions": 0, "total_users": 0,
            "uptime_days": 0, "speed_mbps": 0.0, "score": 0.0}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
RE_REMOTE = re.compile(r"^\s*remote\s+(\S+)\s+(\d+)\s*$", re.M)
RE_PROTO = re.compile(r"^\s*proto\s+(\S+)", re.M)
RE_SESSIONS = re.compile(r"(\d+)\s+SESSIONS")
RE_DAYS = re.compile(r"(\d+)\s+DAYS")
RE_TOTAL_USERS = re.compile(r"TOTAL\s+([\d,]+)\s+USERS")
RE_TCP = re.compile(r"TCP\s*:\s*(\d+)", re.I)
RE_UDP = re.compile(r"UDP\s*:\s*(\d+)", re.I)
RE_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def log(msg):
    print(datetime.now(timezone.utc).strftime("[%H:%M:%S]") + " " + msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_text(v):
    return (v or "").strip() if isinstance(v, str) else ""


def parse_ovpn(text):
    out = {}
    m = RE_REMOTE.search(text)
    if m:
        out["ip"] = m.group(1)
        out["port"] = int(m.group(2))
    m = RE_PROTO.search(text)
    if m:
        out["proto"] = m.group(1).lower()
    for key, rx in (("sessions", RE_SESSIONS), ("days", RE_DAYS),
                    ("users", RE_TOTAL_USERS), ("tcp", RE_TCP), ("udp", RE_UDP)):
        mm = rx.search(text)
        if mm:
            out[key] = int(mm.group(1).replace(",", ""))
    return out


def fetch_official_csv():
    """Official CSV endpoint; each row carries a base64 .ovpn blob."""
    raw = http_get(CSV_URL)
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("#") or not line.strip() or line.startswith("HostName"):
            continue
        rows.append(line)
    out = []
    reader = csv.reader(io.StringIO("\n".join(rows)))
    for row in reader:
        if len(row) < 15:
            continue
        rec = {
            "ip": clean_text(row[1]),
            "score": _to_float(row[2]),
            "speed_mbps": _to_float(row[3]) / 1e6 if row[3] else 0.0,
            "country_long": clean_text(row[5]),
            "country": clean_text(row[6]),
            "sessions": _to_int(row[7]),
            "total_users": _to_int(row[8]),
            "uptime_days": _to_int(row[9]),
            "operator": clean_text(row[10]),
            "tcp_port": 443, "udp_port": 1194,
        }
        ovpn = ""
        try:
            for cell in row:
                if cell.startswith("OpenVPN_ConfigData_Base64"):
                    continue
                try:
                    decoded = base64.b64decode(cell).decode("utf-8", "replace")
                    if "remote " in decoded:
                        ovpn = decoded
                        break
                except Exception:
                    pass
        except Exception:
            pass
        p = parse_ovpn(ovpn)
        if p.get("ip"):
            rec["ip"] = p["ip"]
        if RE_IP.match(rec.get("ip", "")):
            out.append(rec)
    return out


def _rc4(key, data):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 255])
    return bytes(out)


def fetch_official_client():
    """Official client data-file endpoint (RC4/SHA1-obfuscated payload).

    The obfuscation is a documented property of the official client protocol;
    no credentials or decompiled keys are involved.
    """
    raw = http_get(CLIENT_URL)
    key = hashlib.sha1(b"vpngate-client-data").digest()[:16]
    data = _rc4(key, raw)
    text = data.decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not RE_IP.match(parts[0]):
            continue
        rec = {"ip": parts[0], "tcp_port": 0, "udp_port": 0}
        for p in parts[1:]:
            if ":" in p:
                k, v = p.split(":", 1)
                if k in ("tcp", "udp"):
                    rec[k + "_port"] = _to_int(v)
                elif k in DATA_FIELDS:
                    rec[k] = clean_text(v)
        if rec["tcp_port"] or rec["udp_port"]:
            out.append(rec)
    return out


def fetch_inbox():
    """Local manual drops: one host (optionally host:port) per line."""
    out = []
    try:
        names = sorted(os.listdir(INBOX_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".txt"):
            continue
        try:
            with open(os.path.join(INBOX_DIR, name), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        host, port = line.rsplit(":", 1)
                        rec = {"ip": host, "tcp_port": _to_int(port), "udp_port": 0}
                    else:
                        rec = {"ip": line, "tcp_port": 443, "udp_port": 1194}
                    if RE_IP.match(rec["ip"]):
                        out.append(rec)
        except OSError:
            continue
    return out


FETCHERS = {"official_csv": fetch_official_csv,
            "official_client": fetch_official_client,
            "inbox": fetch_inbox}


def _to_int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _to_float(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def merge_sources(records_by_source):
    merged = {}
    for source in SOURCE_ORDER:
        for rec in records_by_source.get(source, []):
            ip = rec.get("ip")
            if not ip:
                continue
            cur = merged.get(ip)
            if cur is None:
                base = dict(DEFAULTS)
                base.update({k: v for k, v in rec.items() if k in DATA_FIELDS})
                base["sources"] = [source]
                merged[ip] = base
            else:
                if source not in cur["sources"]:
                    cur["sources"].append(source)
                for k, v in rec.items():
                    if k in DATA_FIELDS and not cur.get(k) and v:
                        cur[k] = v
    return merged

async def probe(ip, port):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=PROBE_TIMEOUT)
        writer.close()
        return True
    except Exception:
        return False


async def probe_all(nodes):
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def one(rec):
        async with sem:
            rec["alive"] = await probe(rec["ip"], rec["tcp_port"] or rec["udp_port"] or 443)
    await asyncio.gather(*(one(n) for n in nodes))


def rank(nodes):
    for n in nodes:
        n["score"] = round(
            (100.0 if n.get("alive") else 0.0)
            + min(n.get("sessions", 0), 100) * 0.1
            + min(n.get("speed_mbps", 0.0), 100.0) * 0.2, 2)
    return sorted(nodes, key=lambda n: -n["score"])


def build_publish(nodes, require_speed):
    pub = [n for n in nodes if n.get("alive")]
    if require_speed:
        pub = [n for n in pub if n.get("sp_ewma", 0) > 0]
    return {"stamp": now_iso(), "count": len(pub), "nodes": pub[:PUBLISH_MAX]}


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def publish(nodes):
    os.makedirs(PUBLISH_DIR, exist_ok=True)
    # Verified-only set feeds the mihomo provider.
    atomic_write(os.path.join(PUBLISH_DIR, "hub-nodes.json"),
                 build_publish(nodes, require_speed=True))
    # Full alive roster feeds the speed daemon, so a new/unmeasured node can
    # still be discovered (this file is deliberately NOT speed-filtered).
    atomic_write(os.path.join(PUBLISH_DIR, "alive-nodes.json"),
                 build_publish(nodes, require_speed=False))
    atomic_write(os.path.join(PUBLISH_DIR, "hub-stats.json"),
                 {"stamp": now_iso(), "total": len(nodes),
                  "alive": sum(1 for n in nodes if n.get("alive"))})
    atomic_write(STATE_PATH, {"stamp": now_iso(), "nodes": nodes})
    log("publish: %d nodes, %d alive -> %s"
        % (len(nodes), sum(1 for n in nodes if n.get("alive")), PUBLISH_DIR))


def run_once():
    records_by_source = {}
    for source in SOURCE_ORDER:
        try:
            records_by_source[source] = FETCHERS[source]()
            log("%s: %d records" % (source, len(records_by_source[source])))
        except Exception as exc:
            log("%s: fetch failed (%s), continuing" % (source, exc))
            records_by_source[source] = []
    merged = merge_sources(records_by_source)
    nodes = list(merged.values())
    asyncio.run(probe_all(nodes))
    publish(rank(nodes))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="official-only VPNGate node hub")
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    args = ap.parse_args(argv)
    if args.once:
        return run_once()
    log("long-running mode not used; pass --once (systemd timer drives rounds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
