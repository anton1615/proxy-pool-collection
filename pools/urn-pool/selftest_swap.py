#!/usr/bin/env python3
"""swap_backend 的自檢：第一次起不來時要換一顆帳號重試，成功後舊帳號要被釋放。"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from urnpool import Wrapper  # noqa: E402


class FakeStore:
    def __init__(self):
        self.freed = []

    def next_port(self, _field, _base):
        return 50001

    def put_instance(self, inst):
        pass

    def set_account_state(self, token, state, assigned_to=""):
        self.freed.append((token, state))


w = object.__new__(Wrapper)
w.instances = {}
w.store = FakeStore()
released = []
w.release_tokens = lambda toks: released.extend(toks)
w.sweep_orphans = lambda: 0
w.allocate_tokens = lambda uid, n: ["B"]
w.args = types.SimpleNamespace(log_dir="/tmp/sweeptest/swap")

spawned = []


async def spawn_backend(inst, tokens, port):
    spawned.append(list(tokens))
    return types.SimpleNamespace(stop=lambda: None, proc=None)


w.spawn_backend = spawn_backend

calls = {"n": 0}


async def wait_ready(port, timeout=None, proc=None):
    calls["n"] += 1
    return calls["n"] >= 2        # 第一次失敗、第二次成功


w.wait_ready = wait_ready
inst = types.SimpleNamespace(uid="t", backend=None, front=None, tokens=["A"],
                             internal_port=1, country="", proxy="",
                             created_at=0.0, rotated_at=0.0)

ok = asyncio.run(w.swap_backend(inst, ["A"], 111))

assert ok, "swap should have succeeded on the retry"
assert spawned == [["A"], ["B"]], f"expected retry with a fresh account, got {spawned}"
assert inst.tokens == ["B"], inst.tokens
assert released == ["A"], released
print("selftest OK: 429-like failure retried with a fresh account, old account released")
