#!/usr/bin/env python3
"""sweep_orphans 的自檢：假 urnproxy 子進程，孤兒要死、live 的要活。

假進程用 python 跑（argv[0] 覆寫成 urnproxy），因為本機 /bin/sleep 是 coreutils
multi-call 二進位，改名後會因 argv[0] 分派失敗。
"""
import os
import subprocess
import sys
import types
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from urnpool import Wrapper  # noqa: E402

HERE = "/tmp/sweeptest"
os.makedirs(HERE, exist_ok=True)
SCRIPT = os.path.join(HERE, "fake.py")
with open(SCRIPT, "w") as fh:
    fh.write("import time\ntime.sleep(120)\n")


def spawn(port):
    # argv[0] = "urnproxy"（sweep 靠它辨識），真正執行的是 python
    return subprocess.Popen(["urnproxy", SCRIPT, "--addr", f"127.0.0.1:{port}"],
                            executable=sys.executable)


live_port, orphan_port = 39998, 39999
live, orphan = spawn(live_port), spawn(orphan_port)
time.sleep(0.5)

w = object.__new__(Wrapper)
w.instances = {"t": types.SimpleNamespace(internal_port=live_port)}

killed = w.sweep_orphans()

assert killed == 1, f"expected 1 kill, got {killed}"
# sweep 會 waitpid 回收，Popen 的狀態因此不可靠，直接看 /proc
assert not os.path.exists(f"/proc/{orphan.pid}"), "orphan survived"
assert live.poll() is None, "live backend was killed"
live.kill()
print(f"selftest OK: killed={killed}, orphan dead, live backend untouched")
