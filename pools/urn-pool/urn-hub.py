#!/usr/bin/env python3
"""urn-hub: enumerate all online URnetwork providers globally and publish the
open set for the proxy pool.

One round (systemd oneshot + 30 min timer):
  1. re-page /network/find-providers2 for every country (full, no exclude;
     shortcut: full re-page instead of incremental-exclude so offline
     providers age out of last_seen naturally, ~10-25 min wall)
  2. upsert into hub.sqlite (client_id PK; speed/exit state written by
     urn-speed.py is never touched here)
  3. publish hub-open.json = seen this round AND not known-dead AND exit
     risk < 70; urnpool.py /rotate reads it for whole-pool random picks
"""
import faulthandler
import json
import os
import random
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request

faulthandler.register(signal.SIGUSR1)   # journalctl 抓不到卡點時 kill -USR1 看堆疊

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "hub.sqlite")
POOL = os.path.join(HERE, "pool.txt")
SPEED_POOL = os.path.join(HERE, "speed-pool.txt")
OPEN = os.path.join(HERE, "hub-open.json")
API = "https://api.bringyour.com"
PAGE = 100
PAGE_SLEEP = 0.2          # per-page pacing against bringyour.com rate limits
BACKOFFS_MAX = 10
RISK_MAX = 70             # exit risk above this stays out of the open set
DEAD_FAILS = 3            # sp_fail >= this = daemon-proven dead
SHRINK_RATIO = 0.5        # open set collapsed to <50% of previous: keep previous


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def load_seedphrases() -> list[str]:
    out: list[str] = []
    for path in (SPEED_POOL, POOL):
        try:
            with open(path, encoding="utf-8") as fh:
                out += [ln.strip() for ln in fh if ln.strip()]
        except FileNotFoundError:
            pass
    return out


def login(seedphrase: str) -> str:
    body = json.dumps({"seedphrase": seedphrase}).encode()
    req = urllib.request.Request(
        API + "/auth/login", data=body,
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))["network"]["by_jwt"]


def api(jwt: str, path: str, body: dict | None = None,
        timeout: float = 60) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + jwt})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


class JwtPool:
    """Cached logins across the whole account pool: enumeration rate limits
    are per-account, so every page/backoff takes a random account."""

    def __init__(self, conn: sqlite3.Connection, seeds: list[str]):
        self.conn = conn
        self.seeds = seeds
        try:
            self.cache = json.loads(conn.execute(
                "SELECT value FROM meta WHERE key='jwt_cache'").fetchone()[0])
        except (sqlite3.Error, ValueError, TypeError, IndexError):
            self.cache = {}

    def get(self) -> str:
        for seed in random.sample(self.seeds, len(self.seeds)):
            jwt, exp = self.cache.get(seed, ("", 0))
            if jwt and exp > time.time() + 300:
                return jwt
            try:
                jwt = login(seed)
            except Exception:  # noqa: BLE001
                continue
            self.cache[seed] = (jwt, time.time() + 29 * 86400)
            if len(self.cache) > 2000:
                for k in list(self.cache)[:500]:
                    self.cache.pop(k, None)
            self.conn.execute(
                "INSERT INTO meta VALUES('jwt_cache',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(self.cache),))
            self.conn.commit()
            return jwt
        raise LoginError("no account could log in")


class LoginError(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
  client_id TEXT PRIMARY KEY,
  cc TEXT NOT NULL,
  city TEXT DEFAULT '',
  region TEXT DEFAULT '',
  tier INTEGER DEFAULT 0,
  bps INTEGER DEFAULT 0,
  first_seen REAL,
  last_seen REAL,
  last_changed REAL,
  sp_ewma REAL, sp_n INTEGER DEFAULT 0, sp_last REAL,
  sp_ts REAL, sp_fail INTEGER DEFAULT 0,
  u_ok INTEGER DEFAULT 0, alive INTEGER DEFAULT 0,
  rtt_ms REAL, exit_ip TEXT);
CREATE INDEX IF NOT EXISTS idx_prov_due ON providers(sp_ts);
CREATE INDEX IF NOT EXISTS idx_prov_cc ON providers(cc);
CREATE TABLE IF NOT EXISTS exits(
  ip TEXT PRIMARY KEY, cc TEXT, isp TEXT, org TEXT, risk INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS runs(
  ts REAL PRIMARY KEY, wall REAL, fetched INTEGER, total INTEGER,
  online INTEGER, open_n INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

UPSERT = """
INSERT INTO providers(client_id, cc, city, region, tier, bps,
                      first_seen, last_seen, last_changed)
VALUES(?,?,?,?,?,?,?, ?, ?)
ON CONFLICT(client_id) DO UPDATE SET
  cc=excluded.cc, tier=excluded.tier, bps=excluded.bps,
  last_seen=excluded.last_seen,
  last_changed=CASE WHEN providers.city<>excluded.city
                     OR providers.region<>excluded.region
                     OR providers.tier<>excluded.tier
                    THEN excluded.last_seen ELSE providers.last_changed END,
  city=excluded.city, region=excluded.region
"""


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def page_country(conn: sqlite3.Connection, jwts: JwtPool, loc: dict) -> int:
    """Full re-page of one country; returns how many providers are online."""
    cc = (loc.get("country_code") or "").upper()
    n = 0
    backoffs = 0
    rows: list[tuple] = []
    while True:
        # find-providers2 的分頁游標就是 exclude_client_ids：帶上目前為止看過的
        # 全部 id，API 才會往清單深處走；回空 = 這國翻完
        body = {"specs": [{"location_id": loc["location_id"]}], "count": PAGE,
                "exclude_client_ids": [r[0] for r in rows]}
        try:
            got = api(jwts.get(), "/network/find-providers2", body).get("providers") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and backoffs < BACKOFFS_MAX:
                backoffs += 1
                log(f"  {cc}: 429, new account, backing off ({backoffs}/{BACKOFFS_MAX})")
                time.sleep(10)
                continue
            raise
        backoffs = 0
        if not got:
            break
        before = len(rows)
        now = time.time()
        for p in got:
            pl = p.get("location") or {}
            rows.append((p["client_id"], cc, pl.get("city", ""),
                         pl.get("region", ""), int(p.get("tier") or 0),
                         int(p.get("estimated_bytes_per_second") or 0),
                         now, now, now))
        n += len(got)
        if n % 500 < len(got):
            log(f"  {cc}: {n} paged so far")
        if len(rows) == before:
            log(f"  {cc}: page added no new id, stopping")
            break
        if len(got) < PAGE:
            break
        time.sleep(PAGE_SLEEP)
    conn.executemany(UPSERT, rows)
    conn.commit()
    return n


def publish(conn: sqlite3.Connection, round_start: float) -> int:
    rows = conn.execute(
        """SELECT p.client_id, p.cc, p.city, p.region, p.sp_ewma, e.risk,
                  p.sp_fail, p.sp_ts
           FROM providers p LEFT JOIN exits e ON e.ip = p.exit_ip
           WHERE p.last_seen > ? AND p.sp_fail < ?
             AND (e.risk IS NULL OR e.risk < ?)
           ORDER BY p.cc, p.sp_ewma DESC""",
        (round_start - 90, DEAD_FAILS, RISK_MAX)).fetchall()
    doc = {"updated_at": time.time(),
           "providers": [{"id": r[0], "cc": r[1], "city": r[2],
                          "region": r[3],
                          "sp": round(r[4], 1) if r[4] is not None else None,
                          "risk": r[5],
                          "fail": r[6],
                          "sp_ts": int(r[7]) if r[7] is not None else None}
                         for r in rows]}
    # shrink guard: a broken round must not gut the open set
    try:
        with open(OPEN, encoding="utf-8") as fh:
            prev = len(json.load(fh).get("providers", []))
    except (OSError, ValueError):
        prev = 0
    if prev >= 50 and len(rows) < prev * SHRINK_RATIO:
        log(f"publish: open set {len(rows)} < 50% of previous {prev}; keeping previous")
        return prev
    tmp = OPEN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, OPEN)
    return len(rows)


def main() -> int:
    seeds = load_seedphrases()
    if not seeds:
        log("no seedphrases (pool.txt empty?)")
        return 1
    conn = open_db()
    round_start = time.time()
    try:
        jwts = JwtPool(conn, seeds)
        locations = [l for l in api(jwts.get(), "/network/provider-locations")
                     .get("locations", [])
                     if l.get("location_type") == "country"
                     and l.get("provider_count")]
    except LoginError as exc:
        log(str(exc))
        return 1
    log(f"round: {len(locations)} locations")
    fetched = 0
    for loc in sorted(locations, key=lambda l: -l["provider_count"]):
        cc = (loc.get("country_code") or loc.get("name") or "?").upper()
        try:
            n = page_country(conn, jwts, loc)
            fetched += n
            if n:
                log(f"  {cc}: {n} online ({loc['provider_count']} registered)")
        except Exception as exc:  # noqa: BLE001
            log(f"  {cc}: failed: {exc}")
    open_n = publish(conn, round_start)
    total = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
    online = conn.execute(
        "SELECT COUNT(*) FROM providers WHERE last_seen > ?",
        (round_start - 90,)).fetchone()[0]
    conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                 (time.time(), time.time() - round_start, fetched, total,
                  online, open_n))
    conn.commit()
    log(f"round done: fetched {fetched}, db {total}, online {online}, "
        f"open {open_n}, wall {time.time() - round_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
