#!/usr/bin/env python3
"""shared-mihomo register API: uid -> port(7911-7930) + 隨機釘節點 + 節點操作.

  GET /register?uid=N     -> 配一個空閒 port,釘一顆隨機活節點
  GET /rotate?uid=N       -> 原 port 重擲一顆隨機節點
  GET /node?uid=N&name=X  -> 釘指定節點
  GET /nodes              -> provider 全部節點名
  GET /status             -> 現有 uid 對照表
  GET /delete?uid=N       -> 釋放 port
"""
import json, os, random, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("SHARED_MIHOMO_REG_STATE", os.path.join(BASE, "register.json"))
API = os.environ.get("SHARED_MIHOMO_API", "http://127.0.0.1:7910")
API_HOST = API.split("//", 1)[-1].split(":")[0]
PORTS = list(range(int(os.environ.get("SHARED_MIHOMO_PORT_BASE", "7911")),
                   int(os.environ.get("SHARED_MIHOMO_PORT_END", "7931"))))
# rotate 後斬掉這個 port 上閒置的 keep-alive 連線,讓下一個請求必走新節點(新出口 IP)。
# KILL_RESP_GRACE 必須大於上游最大 token 間隔(實測 6s),否則活串流會被誤砍。
KILL_RESP_GRACE = float(os.environ.get("KILL_RESP_GRACE", 15))
KILL_REQ_GRACE = float(os.environ.get("KILL_REQ_GRACE", 30))
# 每條連線的下載量最後一次變化的時間(mihomo 只有總計數器,沒有方向/時間戳)
_IDLE = {}
LOCK = threading.Lock()
_log = lambda *a: print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def _state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

def _save(d):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)

def _ctrl(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(API + path, data=data or b"",
                                 method=method or ("PUT" if payload is not None else "GET"),
                                 headers={"Content-Type": "application/json"} if payload else {})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else {}

def _nodes():
    d = _ctrl("/providers/proxies/hub")
    return d.get("proxies", [])  # single-provider path returns the provider object itself

def _node_names():
    return [p["name"] for p in _nodes()]

def _pin(port, name=None, alive_only=True):
    nodes = _nodes()
    names = [p["name"] for p in nodes]
    if alive_only:
        al = [p["name"] for p in nodes if p.get("alive", False)] or names
    else:
        al = names
    if not al:
        raise RuntimeError("no provider nodes")
    pick = name if name else random.choice(al)
    if name and name not in names:
        raise KeyError("node not exist: %s" % name)
    _ctrl("/proxies/p%d" % port, {"name": pick})
    return pick

def _idle_tracker():
    """背景輪詢:記住每條連線的下載量最後一次變化的時間。

    沒有這個 tracker 就分不出「活串流(token 之間 download 沒變)」與
    「閒置池(下載早就停了)」——mihomo 不給 per-connection 的流量時間。
    """
    while True:
        try:
            now = time.time()
            live = {}
            for c in _ctrl("/connections").get("connections") or []:
                live[c.get("id")] = (c.get("upload", 0), c.get("download", 0))
            with LOCK:
                for cid, (up, down) in live.items():
                    tr = _IDLE.get(cid)
                    if tr is None or tr["down"] != down:
                        _IDLE[cid] = {"last_down": now, "down": down}
                for cid in list(_IDLE):
                    if cid not in live:
                        _IDLE.pop(cid, None)
        except Exception as e:
            msg = repr(e)
            if msg != _idle_tracker.last_error:
                _log("idle tracker controller error:", msg)
                _idle_tracker.last_error = msg
        time.sleep(1.0)


_idle_tracker.last_error = None


def _kill_idle_conns(port):
    """關掉這個 listener 上閒置過久的連線(rotate 後讓下一個請求走新節點)。

    mihomo 只給 upload/download 總計數器,沒有方向也沒有「最後流量時間」。
    所以背景 task _idle_tracker 每秒輪詢一次,記住每條連線的下載量最後一次
    變化的時間點:
      - download 持續增加 -> 上游還在說話(活串流)-> 永不斬
      - download 停止增加 >= KILL_RESP_GRACE -> 回應結束、閒置池 -> 斬
      - download == 0 且 upload > 0 -> 請求已送、等首 byte(慢 TTFB) -> 保護
    KILL_RESP_GRACE 必須大於上游的 token 間隔(實測最大 6s),否則活串流
    會在兩 token 之間被誤判為閒置。
    """
    now = time.time()
    killed = 0
    for c in _ctrl("/connections").get("connections") or []:
        md = c.get("metadata") or {}
        if md.get("specialProxy") != "p%d" % port:
            continue
        cid = c.get("id")
        up, down = c.get("upload", 0), c.get("download", 0)
        tr = _IDLE.get(cid)
        if tr is None:
            continue                       # 剛建立,tracker 還沒看到
        if down == 0 and up > 0:
            continue                       # 等首 byte:保護
        if down == 0:
            continue                       # 還沒送出任何東西
        idle = now - tr["last_down"]
        if idle < KILL_RESP_GRACE:
            continue                       # 上游最近還在吐東西:活串流
        try:
            _ctrl("/connections/" + cid, method="DELETE")
            killed += 1
            _log("port %d: closed idle conn %s (download idle %.0fs)" % (port, cid, idle))
        except Exception as e:
            _log("port %d: close conn %s failed: %r" % (port, cid, e))
    return killed

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        uid = q.get("uid", "").strip()
        try:
            if u.path == "/nodes":
                names = _node_names()
                return self._send(200, {"count": len(names), "nodes": names})
            with LOCK:
                st = _state()
                if u.path == "/status":
                    out = {k: {"port": v["port"], "node": v.get("node"), "added": v.get("added")} for k, v in st.items()}
                    return self._send(200, {"count": len(out), "clients": out})
                if not uid:
                    return self._send(400, {"error": "uid required"})
                if u.path == "/register":
                    if uid in st:
                        return self._send(200, {"uid": uid, "port": st[uid]["port"], "note": "already registered",
                                                "proxy": "http://%s:%d" % (API_HOST, st[uid]["port"])})
                    used = {v["port"] for v in st.values()}
                    free = [p for p in PORTS if p not in used]
                    if not free:
                        return self._send(503, {"error": "all ports taken", "count": len(st)})
                    port = random.choice(free)
                    node = _pin(port)
                    st[uid] = {"port": port, "node": node, "added": time.time()}
                    _save(st)
                    _log("%s: -> port %d, node %s" % (uid, port, node))
                    return self._send(200, {"uid": uid, "port": port, "node": node,
                                            "proxy": "http://%s:%d" % (API_HOST, port),
                                            "controller": API})
                if uid not in st:
                    return self._send(404, {"error": "not registered", "uid": uid}) if u.path != "/delete" else self._send(200, {"deleted": False})
                if u.path == "/rotate":
                    st[uid]["node"] = _pin(st[uid]["port"]); _save(st)
                    _log("%s: rotate -> %s" % (uid, st[uid]["node"]))
                    killed = _kill_idle_conns(st[uid]["port"])
                    return self._send(200, {"uid": uid, "port": st[uid]["port"], "node": st[uid]["node"],
                                            "killed_idle_conns": killed})
                if u.path == "/node":
                    st[uid]["node"] = _pin(st[uid]["port"], q.get("name")); _save(st)
                    killed = _kill_idle_conns(st[uid]["port"])
                    return self._send(200, {"uid": uid, "port": st[uid]["port"], "node": st[uid]["node"],
                                            "killed_idle_conns": killed})
                if u.path == "/delete":
                    del st[uid]; _save(st)
                    _log("%s: deleted" % uid)
                    return self._send(200, {"deleted": True})
                return self._send(404, {"error": "no such path"})
        except (KeyError, RuntimeError) as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:
            _log("ERROR", u.path, repr(e))
            return self._send(502, {"error": repr(e)})
    do_POST = do_GET

if __name__ == "__main__":
    host = os.environ.get("SHARED_MIHOMO_REG_BIND", "127.0.0.1")
    port = int(os.environ.get("SHARED_MIHOMO_REG_PORT", "8095"))
    threading.Thread(target=_idle_tracker, daemon=True).start()
    _log("shared-mihomo register api on %s:%d" % (host, port))
    ThreadingHTTPServer((host, port), H).serve_forever()
