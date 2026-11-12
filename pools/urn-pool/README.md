# urn-pool

**One shared URnetwork account pool, many clients.** `urn-pool` turns
[urnproxy](https://github.com/urnetwork/proxy) — the URnetwork SOCKS5 `socks`
adapter, patched to log in with a **seedphrase** instead of email/password —
into a small **multi-client service for your trusted network**: every client
gets its own URnetwork exit behind a stable port, delivered as 🧾 mihomo
subscription nodes, controlled over a ⚙️ HTTP API, and continuously
quality-rated by a resident 📡 hub + speed daemon. Unofficial community
software. Not affiliated with or endorsed by URnetwork.
> Only use it with accounts you are authorized to use, in accordance with
> URnetwork's terms of service.

Design borrowed heavily from
[foxyvpn-pool](https://github.com/anton1615/foxyvpn-pool) — same byte-relay
swap choreography, adapted from token pools to seedphrase accounts.

## ✨ Highlights

- 🖥 **One pool, N clients** — every client draws from the same
  pre-provisioned seedphrase account pool on the host
- 🚪 **A stable port per client** — public ports come from a monotonic counter
  persisted in the state DB, so subscription URLs never go stale
- 🌍 **Region switching / rotation nobody notices** — `/country` and `/rotate`
  warm up the replacement backend first; existing connections keep flowing
  while the old one drains
- 🔌 **SOCKS5 *and* HTTP CONNECT on the same port** — the front sniffs the
  first byte, so a proxy-only consumer (curl `--proxy http://…`, many
  scrapers) needs no converter
- 🧾 mihomo subscription endpoints (`/sub`, `/sub?uid=…`) plus plain SOCKS5
- 🔋 **Quota self-management** — each account gets a free daily grant
  (~30 GiB, UTC 00:00); the wrapper probes `/subscription/balance` every
  cycle, swaps a client to a fresh account *before* its current one runs dry,
  and dead accounts revive automatically when the next grant lands
- 📡 **Global provider hub** — `urn-hub.py` re-enumerates online providers
  across all countries every 30 min and publishes an "open set"
  (fresh ∧ not daemon-proven-dead ∧ low exit risk) that `/rotate` draws from
- ⚡ **Speed daemon** — `urn-speed.py` dials providers one by one, measures
  liveness + median throughput, and rates each exit IP (proxy/hosting/mobile)
  into the hub DB
- ⚙️ Full instance lifecycle from the API: register, delete, list locations,
  switch country, rotate, status
- 🎛 **Dashboard included** — every client as a card (port, exit, quota left,
  relays) with create / rotate / delete buttons; one rotate button per card,
  no country picker

## 🧠 How it works

```
client ──socks5h / HTTP CONNECT──> <bind-host>:2100x  🚪 front (terminates the handshake)
                                           │
              old backend keeps serving its connections ◄── 🔁 flip
                                           │                (drain, then retire)
                                 127.0.0.1:3100x  urnproxy backend (1 account)
                                           │
                                  api.bringyour.com ──► 🌍 URnetwork exit
```

Every client (a `uid` of its choosing) gets a **public port** (`21000+`, bound
to the network address, stable for its lifetime) served by a byte-relay, and
an **internal urnproxy process** (`127.0.0.1:31000+`, `--provider-id` or
`--country` pinned) holding one account from the pool.

The front accepts **both SOCKS5 and HTTP proxy (`CONNECT`) clients on the
same port**, so a client that only speaks HTTP proxy needs no socks5→http
converter. It peeks at the first byte: `0x05` → it completes the SOCKS5
handshake itself; `C` → it reads the request line, requires `CONNECT
host:port`, dials it and answers `200 Connection Established`; anything else
gets `405` and the socket closes. Either way it then dials the backend's
SOCKS5 port on the client's behalf and moves bytes.

The key trick (inherited from foxyvpn-pool): SOCKS5 carries exactly **one
tunnel per TCP connection**, so the front only has to understand the
handshake — after it, every client connection is independent and a swap only
affects connections that arrive after it.

## 🔑 The seedphrase account pool

- `pool.txt` holds your account **seedphrases**, one per line; `auth/*.json`
  holds per-account metadata (`seedphrase`, `network_id`, `network_name`).
  Both are re-read every poll cycle — appending to the file is enough, no
  restart needed.
- **Requests are authenticated with the seedphrase itself**: the wrapper
  `POST /auth/login {"seedphrase": …}` → `by_jwt` (JWT, cached in memory and
  across the shared DB for ~29 days), then carries the JWT on
  `/subscription/balance`, `/network/find-providers2`, etc.
- Accounts newer than the pool are picked up automatically. Registration
  tooling (minting new accounts) is **not** part of this repo — bring your
  own pool file.
- ⚠️ A seedphrase *is* the account credential: `pool.txt`, `auth/` and the
  state DBs must stay `0600` and never be committed.

## 🤖 The hub and the speed daemon

- **`urn-hub.py`** (oneshot + timer, 30 min): pages `find-providers2` for
  every country — the pagination cursor is `exclude_client_ids`, so each page
  must exclude what it already saw or the API returns the same top-100
  forever — upserts providers into `hub.sqlite`, and publishes
  `hub-open.json`: providers seen this round, not speed-failed ≥3 times, and
  exit risk < 70. A shrink guard keeps the previous file if the fresh one
  collapses to <50% (API hiccup protection).
- **`urn-speed.py`** (resident): picks the least-recently-tested provider,
  dials it through a scratch urnproxy, checks SOCKS5 liveness, measures a
  median of 3×8 MB downloads, looks up the exit IP's hosting/proxy/mobile
  flags as a risk score (cached 24 h per IP), and writes everything back to
  the hub DB. Bandwidth is capped by `SPEED_DAILY_GB` (default 300 GB/day).
- **`urnpool.py` `/rotate`** draws from the open set when it is fresh
  (<65 min), verifies the candidate actually answers before committing, and
  retries up to 3 times; otherwise it falls back to weighted random country
  selection.

## 🚀 Install

stdlib-only Python 3.11+; no venv strictly needed.

```bash
git clone <this repo> ~/urn-pool
# build the adapter once:
git clone https://github.com/urnetwork/proxy urn-proxy && cd urn-proxy
# patch login to seedphrase: POST /auth/login {"seedphrase": "..."}
go build -o urnproxy ./socks
cp urnproxy ~/urnproxy            # default: $URNPROXY, else ~/urnproxy
# or point the units elsewhere:  --urnproxy /path/to/urnproxy  /  URNPROXY=/path/to/urnproxy
```

Configure the account pool (kept next to the code or wherever your units
point):

```bash
cd ~/urn-pool
cp examples/pool.txt.example pool.txt      # your seedphrases, 0600
mkdir -p auth && cp examples/auth.example.json auth/<id>.json
```

Foreground test run:

```bash
python3 urnpool.py                         # pool wrapper + HTTP API
python3 urn-hub.py                         # one enumeration round
python3 urn-speed.py                       # speed daemon (SPEED_DAILY_GB=300)
```

As systemd --user services (see `examples/`):

```bash
cp examples/*.service.example examples/*.timer.example \
   ~/.config/systemd/user/  # rename to drop .example, adjust paths
systemctl --user daemon-reload
systemctl --user enable --now urn-pool urn-speed urn-hub.timer
```

By default the API binds to the machine's Tailscale address (port 8090).
Override with `--bind-host`.

## 🖥️ Web UI

`urnpool.py` ships a dashboard for the same API. Without `--ui-token` it is
served on `/`, which only the tailnet can reach; set `--ui-token` and it moves
to `/<token>/` (the bare root then returns 404). The token is an alias, not a
gate: every API path also answers under it (`/<token>/register`,
`/<token>/sub`, …), while the bare paths keep working unchanged, so existing
subscriptions and clients need no edit.

```bash
python3 ~/urn-pool/urnpool.py --ui-token "$(openssl rand -hex 12)"
# → http://<tailnet-ip>:8090/<token>/
```

The page lists every client with its name, public port, SOCKS5 URL, current
exit (pinned provider or country), account count, remaining/max quota, live
connection count and how long ago it rotated, and offers:

- ➕ create a client from a name alone — the exit is drawn for it, no country
  picker,
- ♻️ **rotate** — one button that runs `/rotate`: a whole-pool weighted random
  provider pick, the new exit is verified to pass traffic before it is
  accepted, and the public port never changes,
- 🗑 delete a client, behind a confirmation dialog.

Quota per client is the sum over its accounts, so exhaustion shows up on the
card before any consumer notices. It refreshes itself every 15 s.

## ⚙️ HTTP API

Example host below: `[IP]` — replace with your own. With `--ui-token TOKEN`
every path in the table also answers under `/<TOKEN>` (`/status` and
`/<TOKEN>/status` are the same endpoint); the bare paths are unchanged.

| Endpoint | Meaning |
|---|---|
| `GET /` | 🖥 Dashboard: cards per client (port, exit, quota left/max, relays), add / delete / rotate buttons. Served under `/<secret>/` when `--ui-token` is set |
| `GET /ui-state` | 📦 JSON the dashboard polls (same shape as `/status`, plus per-client `quota_left_bytes` / `quota_max_bytes`) |
| `GET /register?uid=<name>` | 🚪 Reserve a client: allocate an account, start urnproxy, return its stable port. Idempotent per name. Optional `&country=<Country>` (default United States) or `&provider=<uuid>` to pin one provider |
| `GET /delete?uid=<name>` | 🗑 Tear the instance down, release account and port |
| `GET /sub[?uid=<name>]` | 🧾 mihomo YAML with that node (or every node) |
| `GET /country?uid=<name>&country=<Country>` | 🔁 Move the client to another country, invisibly |
| `GET /country?uid=<name>&provider=<uuid>` | 🎯 Pin an exact upstream provider |
| `GET /rotate?uid=<name>` | ♻️ Whole-pool rotate: weighted random provider pick from the hub open set (cached / pinned country replaced), up to three picks verified before it answers 500 |
| `GET /locations` | 🌍 Available countries (URnetwork provider-locations) |
| `GET /providers?country=us&limit=600` | 📡 Raw provider list for a country |
| `GET /status` | 📊 Per-client state and pool accounting (free/assigned/dead, quota bytes) |

Register a client and use it:

```bash
curl 'http://<host>:8090/register?uid=laptop'
# → {"uid":"laptop","port":21000,"proxy":"socks5://<host>:21000",...}
curl --socks5-hostname <host>:21000 https://api.ipify.org
```

mihomo consumes the subscription URL instead:

```
http://<host>:8090/sub?uid=laptop
```

DNS must resolve on the exit side (`socks5h` semantics): curl uses
`--socks5-hostname`, Firefox sets `network.proxy.socks_remote_dns=true`,
mihomo nodes are plain `socks5` type.

## 🛡 Security & trust model

**Built for a trusted network — a tailnet, a VPN, or a LAN you control.**
The SOCKS5 ports and the bare API paths carry no authentication: the trust
boundary is your network's egress, not this service. Do not expose the bind
address to the public internet.

- Nothing enforces a client↔node mapping: any client on the network can reach
  any node the host has created and steer it via a known `uid`.
- `--ui-token` only moves the dashboard off the bare root: the same actions
  stay reachable at the bare API paths, so it is a convenience URL, not a
  gate. Without it the dashboard sits on `/` for anyone on the network.
- Backend listeners are loopback-only. The urnproxy login passes the
  seedphrase via the `USER_AUTH` environment variable — keep it out of
  shared process listings.
- `pool.txt`, `auth/`, `db.sqlite`, `hub.sqlite` contain account credentials
  and must stay `0600` (they are in `.gitignore`, keep them out of any
  public checkout).

## 🧩 Layout

```
urnpool.py        pool wrapper + HTTP API + byte relay (standalone, stdlib only)
urn-hub.py        global provider enumeration → hub.sqlite → hub-open.json
urn-speed.py      resident liveness/throughput/exit-risk daemon
selftest_*.py     offline self-tests for the swap choreography
examples/         example data formats + systemd --user units
```

## 🙏 Credits

- **urnproxy** — the `socks` adapter from URnetwork's
  [urnetwork/proxy](https://github.com/urnetwork/proxy); this repo patches
  only its login path to accept a seedphrase.
- **foxyvpn-pool** — the byte-relay/warm-up/swap design and README voice are
  borrowed from [anton1615/foxyvpn-pool](https://github.com/anton1615/foxyvpn-pool).
