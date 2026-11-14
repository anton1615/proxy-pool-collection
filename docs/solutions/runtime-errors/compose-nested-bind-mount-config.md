---
title: Nested single-file mount fails inside a read-only bind mount
date: 2026-11-14
category: runtime-errors
module: docker-compose demo stack
problem_type: runtime_error
component: infrastructure
symptoms:
  - "docker compose up starts the mock service but the webui container fails at init"
  - "OCI runtime create failed: runc create failed: unable to start container process: error during container init: error mounting <host-config> to rootfs at /app/docker/config.json: create mountpoint for /app/docker/config.json mount: make mountpoint /app/docker/config.json: openat config.json: read-only file system"
root_cause: config_error
resolution_type: config_change
severity: medium
tags: [docker-compose, bind-mount, read-only, volume, config, container-init]
---

# Nested single-file mount fails inside a read-only bind mount

## Problem

The demo Compose stack would not start its `webui` service: the shell config was mounted as a second single-file volume into a path that lived inside an already read-only bind-mounted directory. Docker could not create the mountpoint, so container init failed before `webui/server.py` ever ran.

## Symptoms

- `docker compose up -d` reports the `mock` container started but `webui` fails with `OCI runtime create failed`.
- The error names `create mountpoint for /app/docker/config.json ... read-only file system`.
- `curl http://127.0.0.1:8787/` returns connection refused (no shell is listening).

## What Didn't Work

- The discarded first attempt (removed by this fix): a second single-file volume mapping the host example config to an in-container `config.json` path, plus an env var pointing `PPC_CONFIG` at that in-container path — both nested under the read-only `.:/app:ro` tree, alongside that same repo bind mount. Compose config validates, but container init fails: `.:/app:ro` makes the whole `/app` tree read-only, and Docker must create the `config.json` mountpoint file inside it before it can bind the second volume. This was observed live, not inferred. Neither the second volume nor that `PPC_CONFIG` value exists in the current tree.
- Treating the failure as a config-content problem. The config JSON was valid; `docker compose config -q` passed. The failure was purely at the mount layer.

## Solution

Point `PPC_CONFIG` at a file that is already inside the single repo bind mount, and keep only that one volume:

```yaml
services:
  webui:
    environment:
      PPC_CONFIG: /app/docker/config.example.json
    volumes:
      - .:/app:ro
```

The current tree does this at `docker-compose.yml:22` (`PPC_CONFIG: /app/docker/config.example.json`) and `docker-compose.yml:24` (`- .:/app:ro`). The shell honors the env override at `webui/server.py:22` (`CONFIG_PATH = os.environ.get("PPC_CONFIG", ...)`), so the example file is read directly — no copy step, no second mount, no build step. Verified with live `docker compose up` smoke in PR #2 (merged).

## Why This Works

A read-only bind mount (`.:/app:ro`) presents `/app` as a read-only filesystem to container init. A nested single-file mount needs Docker to create a mountpoint at `/app/docker/config.json`; on a read-only filesystem that `openat` fails. Referencing a file already present inside the mount (`docker/config.example.json` ships in the repo) removes the need to create anything. `PPC_CONFIG` exists precisely to redirect the config path without touching `webui/config.example.json` or baking config into an image.

## Prevention

- When a Compose service already bind-mounts a directory read-only, do not add a second single-file volume targeting a path inside it. Point the app's config-path env var at a file already in the mount instead.
- `docker compose config -q` does not catch this class of failure — it validates syntax, not container-init mount feasibility. A real `docker compose up` smoke (plus a curl to the served port) is the check that catches it.
- If a separate operator-editable config is genuinely needed, mount it to a path outside the read-only tree (e.g. `/run/config/config.json`) rather than nesting it under `/app`.

## Related Issues

- PR #2 `feat(docker): add one-command compose demo alongside bare-metal launch` (merged) introduced and verified this stack.
- `docker-compose.yml:10-14` documents the chosen shape (zero-prep `PPC_CONFIG`) but not the failure mode; this doc holds the why.
- A gitignored plan under `docs/plans/` recorded the same root cause at planning time. It is not tracked in public Git, so this is the durable home for the lesson.
