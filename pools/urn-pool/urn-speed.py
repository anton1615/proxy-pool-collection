#!/usr/bin/env python3
"""urn-speed: resident daemon; dials URnetwork providers one by one through a
pinned urnproxy, measures exit speed and cleanliness, writes results into
hub.sqlite for urn-hub publishing.

Per test (WORKERS in parallel): spawn `urnproxy --provider-id X` on a private
loopback SOCKS5 port -> cheap alive check (SOCKS5 CONNECT) -> one curl process
with --next (200 KB warm-up + 3x 8 MB downloads, body-only Mbps median) ->
exit IP quality via ip-api (24 h per-IP cache) -> UPDATE providers.

Data budget: ~25 MB per test; SPEED_DAILY_GB (default 300) per UTC day, since
every byte rides a pool account's 30 GiB daily grant.
"""
import json
import os
import queue
import random
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "hub.sqlite")
POOL = os.path.join(HERE, "pool.txt")
SPEED_POOL = os.path.join(HERE, "speed-pool.txt")
POOL_DB = os.path.join(HERE, "db.sqlite")   # urn-pool's store (read-only use)
URNPROXY = os.environ.get("URNPROXY", os.path.expanduser("~/urnproxy"))
# adapter egress proxy ("" = direct). A SOCKS5 hop spreads the spawn/contract
# pressure off this host's address; measurements then include the hop.
EGRESS_PROXY = os.environ.get("URN_SPEED_PROXY", "")
API = "https://api.bringyour.com"

BATCH = 6
WORKERS = 3
PORT_BASE = 21500
MIN_QUOTA = 8 * 1024 ** 3        # drop accounts below this
DAILY_GB = float(os.environ.get("SPEED_DAILY_GB", "300"))
LOAD_LIMIT = 4.0                 # loadavg guard (ib1: 4 cores, shares the pool)
FRESH_SECS = 48 * 3600           # only test providers seen recently
FAIL_MAX = 5                     # consecutive dead dials -> cooldown
FAIL_COOLDOWN = 24 * 3600
READY_TIMEOUT = 45.0
EXIT_TTL = 24 * 3600
LIVE_BYTES = 200_000               # spawn handshake + one ip-api call (~80 KB real)
# fraction of tests that also measure throughput; the rest only refresh
# liveness (alive/sp_fail/rtt) and cleanliness (exit ip/risk)
SPEED_FRACTION = float(os.environ.get("SPEED_MEASURE_FRACTION", "0"))
HOSTING_KEYWORDS = ("hosting", "datacenter", "data center", "cloud",
                    "server", "colo", "vps")


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def open_db() -> "Conn":
    conn = Conn(DB)
    conn.executescript("""
CREATE TABLE IF NOT EXISTS providers(
  client_id TEXT PRIMARY KEY, cc TEXT NOT NULL,
  city TEXT DEFAULT '', region TEXT DEFAULT '',
  tier INTEGER DEFAULT 0, bps INTEGER DEFAULT 0,
  first_seen REAL, last_seen REAL, last_changed REAL,
  sp_ewma REAL, sp_n INTEGER DEFAULT 0, sp_last REAL,
  sp_ts REAL, sp_fail INTEGER DEFAULT 0,
  u_ok INTEGER DEFAULT 0, alive INTEGER DEFAULT 0,
  rtt_ms REAL, exit_ip TEXT);
CREATE INDEX IF NOT EXISTS idx_prov_due ON providers(sp_ts);
CREATE TABLE IF NOT EXISTS exits(
  ip TEXT PRIMARY KEY, cc TEXT, isp TEXT, org TEXT, risk INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
""")
    return conn


class Conn:
    """sqlite3 connection shared across worker threads, fully serialized."""

    def __init__(self, path: str):
        self._c = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()

    def execute(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._c.execute(sql, args)

    def q(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self._c.execute(sql, args).fetchall()

    def commit(self) -> None:
        with self._lock:
            self._c.commit()

    def executescript(self, s: str) -> None:
        with self._lock:
            self._c.executescript(s)


def meta_get(conn: "Conn", key: str) -> str | None:
    rows = conn.q("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0][0] if rows else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def load_jwt_cache(conn: sqlite3.Connection) -> dict:
    try:
        return json.loads(meta_get(conn, "jwt_cache") or "{}")
    except ValueError:
        return {}


def save_jwt_cache(conn: sqlite3.Connection, cache: dict) -> None:
    if len(cache) > 2000:
        for key in list(cache)[:500]:
            cache.pop(key, None)
    meta_set(conn, "jwt_cache", json.dumps(cache))


def login(conn: sqlite3.Connection, jwts: dict, seed: str) -> str:
    jwt, exp = jwts.get(seed, ("", 0))
    if jwt and exp > time.time() + 300:
        return jwt
    body = json.dumps({"seedphrase": seed}).encode()
    req = urllib.request.Request(
        API + "/auth/login", data=body,
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    jwt = resp["network"]["by_jwt"]
    jwts[seed] = (jwt, time.time() + 29 * 86400)
    return jwt


def balance(jwt: str) -> int:
    req = urllib.request.Request(
        API + "/subscription/balance",
        headers={"Authorization": "Bearer " + jwt})
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    return int(resp.get("balance_byte_count") or 0)


class LoginRejected(Exception):
    pass


class Accounts:
    """Speed-test accounts: dedicated speed-pool.txt first, then urn-pool's
    free accounts (their quota_left is the store's last probe)."""

    def __init__(self, conn: sqlite3.Connection, jwts: dict):
        self.conn = conn
        self.jwts = jwts
        self.seeds: list[str] = []
        for path in (SPEED_POOL, POOL):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.seeds += [ln.strip() for ln in fh if ln.strip()]
            except FileNotFoundError:
                pass
        self.skipped: set[str] = set()
        self.since_check = 0

    def pick(self) -> tuple[str, str]:
        pool_free: list[str] = []
        try:
            prown = sqlite3.connect(f"file:{POOL_DB}?mode=ro", uri=True, timeout=5)
            pool_free = [r[0] for r in prown.execute(
                "SELECT token FROM accounts WHERE state='free' "
                "AND quota_left >= ?", (MIN_QUOTA,))]
            prown.close()
        except sqlite3.Error:
            pass
        cands = ([s for s in self.seeds if s not in self.skipped]
                 + [s for s in pool_free if s not in self.skipped])
        random.shuffle(cands)
        for seed in cands[:20]:
            try:
                jwt = login(self.conn, self.jwts, seed)
            except Exception:  # noqa: BLE001  (429 / rejected / timeout)
                self.skipped.add(seed)
                continue
            self.since_check += 1
            if self.since_check >= 10:
                self.since_check = 0
                try:
                    if balance(jwt) < MIN_QUOTA:
                        self.skipped.add(seed)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            return seed, jwt
        raise LoginRejected("no usable account")


def pick_due(conn: "Conn", n: int) -> list[str]:
    now = time.time()
    return [r[0] for r in conn.q(
        """SELECT client_id FROM providers
           WHERE last_seen > ?
           AND NOT (sp_fail >= ? AND sp_ts > ?)
           ORDER BY CASE WHEN sp_ts IS NULL THEN 0 ELSE 1 END, sp_ts ASC
           LIMIT ?""",
        (now - FRESH_SECS, FAIL_MAX, now - FAIL_COOLDOWN, n))]


def socks_alive(port: int, timeout: float = 10.0) -> tuple[bool, float]:
    """SOCKS5 CONNECT to 1.1.1.1:443; returns (alive, rtt_ms)."""
    import socket
    start = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x05\x01\x00")
            if s.recv(2) != b"\x05\x00":
                return False, 0.0
            s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("1.1.1.1")
                      + (443).to_bytes(2, "big"))
            resp = s.recv(4)
            rtt = (time.monotonic() - start) * 1000
            return len(resp) >= 2 and resp[1] == 0, rtt
    except OSError:
        return False, 0.0


CURL_W = "%{time_starttransfer} %{time_total} %{size_download} %{http_code}\\n"


def measure(port: int) -> tuple[float, int] | None:
    """Median body-only Mbps of 3x 8 MB downloads; (mbps, bytes)."""
    url_s = "https://speed.cloudflare.com/__down?bytes="
    cmd = ["curl", "-sS", "-x", f"socks5h://127.0.0.1:{port}", "-o", "/dev/null",
           "-w", CURL_W, "--max-time", "4", url_s + "200000",
           "--next", "-o", "/dev/null", "-w", CURL_W, "--max-time", "8",
           url_s + "8000000",
           "--next", "-o", "/dev/null", "-w", CURL_W, "--max-time", "8",
           url_s + "8000000",
           "--next", "-o", "/dev/null", "-w", CURL_W, "--max-time", "8",
           url_s + "8000000"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=45).stdout
    except subprocess.TimeoutExpired:
        return None
    samples, total_bytes = [], 0
    for idx, line in enumerate(out.splitlines()):
        parts = line.split()
        if len(parts) != 4 or parts[3] != "200":
            continue
        start, total, size = float(parts[0]), float(parts[1]), int(parts[2])
        total_bytes += size
        if idx == 0:
            continue   # warm-up: counts for bytes only
        body_time = total - start
        if body_time > 0 and size > 1_000_000:
            samples.append(size * 8 / body_time / 1e6)
    if not samples:
        return None
    return sorted(samples)[len(samples) // 2], total_bytes


def exit_quality(port: int, conn: sqlite3.Connection) -> tuple[str, int] | None:
    """(exit_ip, risk), cached per IP for EXIT_TTL."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "-x", f"socks5h://127.0.0.1:{port}", "--max-time",
             "10",
             "http://ip-api.com/json?fields=query,countryCode,isp,org,as,"
             "mobile,proxy,hosting"],
            capture_output=True, text=True, timeout=15).stdout
        info = json.loads(out)
    except Exception:  # noqa: BLE001
        return None
    ip = info.get("query", "")
    if not ip:
        return None
    rows = conn.q("SELECT risk, ts FROM exits WHERE ip=?", (ip,))
    if rows and time.time() - rows[0][1] < EXIT_TTL:
        return ip, rows[0][0]
    isp = (info.get("isp") or "") + " " + (info.get("org") or "")
    risk = (45 if info.get("proxy") else 0) + (25 if info.get("hosting") else 0)
    if any(k in isp.lower() for k in HOSTING_KEYWORDS):
        risk += 15
    if info.get("mobile"):
        risk -= 20
    risk = max(0, min(100, risk))
    conn.execute(
        "INSERT INTO exits VALUES(?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET "
        "cc=excluded.cc, isp=excluded.isp, org=excluded.org, "
        "risk=excluded.risk, ts=excluded.ts",
        (ip, info.get("countryCode"), info.get("isp"), info.get("org"),
         risk, time.time()))
    conn.commit()
    return ip, risk


def test_provider(conn: sqlite3.Connection, accounts: Accounts,
                  slot_q: queue.Queue, client_id: str) -> tuple[str, int]:
    """One provider: spawn -> liveness -> cleanliness (-> optional speed).

    Returns (status, bytes spent). Bytes are what the account quota really
    paid for: a live probe is ~LIVE_BYTES, a speed test ~25 MB.
    """
    port = slot_q.get()
    proc = None
    try:
        try:
            seed, jwt = accounts.pick()
        except LoginRejected as exc:
            log(f"  {client_id[:16]}: {exc}")
            return "noaccount", 0
        env = os.environ.copy()
        env["USER_AUTH"] = seed
        # /auth/login is rate limited per network address: reuse the cached by_jwt
        env["BY_JWT"] = jwt
        if EGRESS_PROXY:
            env["URN_PROXY"] = EGRESS_PROXY
        env["PASSWORD"] = "urn-speed"
        proc = subprocess.Popen(
            [URNPROXY, "--addr", f"127.0.0.1:{port}", "--provider-id", client_id],
            stdout=open(f"/tmp/urnspd/adapter-{port}.log","w"), stderr=subprocess.STDOUT,
            start_new_session=True, env=env)
        deadline = time.time() + READY_TIMEOUT
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                import socket
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                time.sleep(0.3)
        if not ready:
            conn.execute(
                "UPDATE providers SET sp_fail=sp_fail+1, sp_ts=?, alive=0 "
                "WHERE client_id=?", (time.time(), client_id))
            conn.commit()
            return "noreach", LIVE_BYTES
        alive, rtt = socks_alive(port)
        if not alive:
            conn.execute(
                "UPDATE providers SET sp_fail=sp_fail+1, sp_ts=?, alive=0 "
                "WHERE client_id=?", (time.time(), client_id))
            conn.commit()
            return "dead", LIVE_BYTES
        # one ip-api call through the exit: exit ip + cleanliness, cached per ip
        eq = exit_quality(port, conn)
        if eq is None:
            conn.execute(
                "UPDATE providers SET sp_fail=sp_fail+1, sp_ts=?, alive=0 "
                "WHERE client_id=?", (time.time(), client_id))
            conn.commit()
            return "no-exit", LIVE_BYTES
        if random.random() >= SPEED_FRACTION:
            # liveness + cleanliness only: keep the last speed score
            conn.execute(
                """UPDATE providers SET sp_ts=?, sp_fail=0, u_ok=1, alive=1,
                     rtt_ms=?, exit_ip=? WHERE client_id=?""",
                (time.time(), rtt, eq[0], client_id))
            conn.commit()
            return "live rtt=%.0fms risk=%d" % (rtt, eq[1]), LIVE_BYTES
        got = measure(port)
        if got is None:
            conn.execute(
                "UPDATE providers SET sp_fail=sp_fail+1, sp_ts=? "
                "WHERE client_id=?", (time.time(), client_id))
            conn.commit()
            return "nospeed", LIVE_BYTES
        mbps, used_bytes = got
        conn.execute(
            """UPDATE providers SET
                 sp_ewma=CASE WHEN sp_ewma IS NULL OR sp_ewma<0 THEN ?
                              ELSE 0.3*? + 0.7*sp_ewma END,
                 sp_n=sp_n+1, sp_last=?, sp_ts=?, sp_fail=0,
                 u_ok=1, alive=1, rtt_ms=?, exit_ip=?
               WHERE client_id=?""",
            (mbps, mbps, mbps, time.time(), rtt,
             eq[0] if eq else None, client_id))
        conn.commit()
        return (f"ok {mbps:.1f}Mbps {used_bytes // 1_000_000}MB risk={eq[1]}",
                used_bytes)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        slot_q.put(port)


def main() -> int:
    conn = open_db()
    jwts = load_jwt_cache(conn)
    accounts = Accounts(conn, jwts)
    slot_q: queue.Queue = queue.Queue()
    for i in range(WORKERS):
        slot_q.put(PORT_BASE + i)
    from concurrent.futures import ThreadPoolExecutor
    log(f"speed daemon up: workers={WORKERS} "
        f"budget={'unlimited' if DAILY_GB <= 0 else '%.0fGB/day' % DAILY_GB} "
        f"egress={EGRESS_PROXY or 'direct'} "
        f"speed_sample={SPEED_FRACTION or 'off'}")
    while True:
        try:
            load1 = float(open("/proc/loadavg").read().split()[0])
            if load1 > LOAD_LIMIT:
                log(f"load {load1} > {LOAD_LIMIT}, idling")
                time.sleep(120)
                continue
            day = time.strftime("%Y-%m-%d", time.gmtime())
            used = int(meta_get(conn, f"bytes:{day}") or 0)
            if DAILY_GB > 0 and used > DAILY_GB * 1e9:
                log(f"daily budget {used / 1e9:.0f}GB reached, idling")
                time.sleep(600)
                continue
            due = pick_due(conn, BATCH)
            if not due:
                time.sleep(300)
                continue
            save_jwt_cache(conn, jwts)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                results = list(pool.map(
                    lambda cid: test_provider(conn, accounts, slot_q, cid), due))
            used += sum(spent for _status, spent in results)
            meta_set(conn, f"bytes:{day}", str(used))
            ok = sum(1 for status, _ in results
                     if status.startswith(("ok", "live")))
            budget = f"{DAILY_GB:.0f}GB" if DAILY_GB > 0 else "unlimited"
            log(f"batch: {ok}/{len(due)} ok-or-live; day {used / 1e9:.2f}GB/"
                f"{budget} | " + "; ".join(s for s, _ in results))
        except Exception as exc:  # noqa: BLE001
            log(f"cycle failed: {exc}")
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
