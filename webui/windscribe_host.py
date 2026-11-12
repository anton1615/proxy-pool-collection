#!/usr/bin/env python3
"""Serve the vendored windscribe host-snapshot dashboard as a standalone runner.

The snapshot is the deployed wspool.py, kept outside the pinned subtree. It is
launched with UI_TOKEN / BIND_HOST / API_PORT so its UI is reachable; all of
its BASE-relative runtime state (accounts, state.json, ...) is gitignored.

This is a thin launcher: it imports the snapshot's module by path and lets its
own HTTP server run. No extra behavior is added.
"""
import os
import runpy
import sys

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "windscribe-host-webui")
SNAPSHOT = os.path.join(SNAPSHOT_DIR, "wspool.py")


def main():
    os.environ.setdefault("UI_TOKEN", "windscribe-dashboard")
    os.environ.setdefault("BIND_HOST", "127.0.0.1")
    os.environ.setdefault("API_PORT", "8096")
    os.chdir(SNAPSHOT_DIR)
    sys.argv = [SNAPSHOT]
    runpy.run_path(SNAPSHOT, run_name="__main__")


if __name__ == "__main__":
    main()
