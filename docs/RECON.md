# Recon: four proxy pools (evidence for planning)

Consolidated from the three upstream GitHub repos, the fourth pool's deployed
service on the operator host, and the local ops-doc snapshots. All hostnames,
IPs, tokens, keys and state paths below are described by **role**; no live
values are recorded here. This file is evidence for the plan, not a spec.

## 1. The four pools

| Pool | Upstream repo | Role | HTTP API (deployed) | Existing web UI |
|---|---|---|---|---|
| foxyvpn-pool | `anton1615/foxyvpn-pool` (public) | shared Mozilla VPN token pool, byte-relay per client, mihomo sub | `:8080` (`wrapper/wrapper.py`) | embedded `UI_HTML` in `wrapper.py`, served under `/<ui-token>/` |
| urn-pool | `anton1615/urn-pool` (public) | shared URnetwork seedphrase pool, hub + speed daemon | `:8090` (`urnpool.py`) | embedded `UI_HTML` in `urnpool.py`, under `/<ui-token>/`; one **Rotate** button per card, no country picker |
| windscribe-pool | `anton1615/windscribe-pool` (public) | shared Windscribe free-account pool, OpenVPN tunnels | `:8094` (`wspool.py`) | **GitHub `main` has NO web UI** (verified: no `UI_HTML` in any commit). The deployed host copy DOES have one (`UI_HTML` + `/<ui-token>/` + `/ui-state`) — drift between repo and deployment. FINDING, see §5. |
| shared-mihomo | none (first-party, deploy-only) | one mihomo instance, N `mixed` listener ports each bound to a `select` group over the VPNGate provider | `:8095` (`register.py`), controller `:7910` | none — must be built |

## 2. How the three existing dashboards attach

All three dashboards use the same single-file pattern (confirmed in source):

- `const BASE = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";`
- `api(action, params)` builds `new URL(BASE + action, location.href)` — **relative to the mounted path prefix**.
- Server strips an optional `/<ui-token>/` prefix before dispatching API paths; bare `/` serves `UI_HTML`; `/ui-state` returns one aggregated JSON blob.

Consequence: each dashboard can be mounted behind a reverse-proxy path prefix
(e.g. `/p/urn/`) without editing its source. That is the "wrap without touching"
mechanism. The unified shell needs to rewrite the HTML base / proxy the API paths.

## 3. shared-mihomo component inventory (what "包進來" means)

From the deployed service and the ops-doc snapshot:

1. **External HTTP API layer** — `register.py` (~207 lines, stdlib only):
   `GET /register|/rotate|/node|/nodes|/status|/delete?uid=`. Maps uid → a free
   port in 7911–7930, pins a random alive provider node via the mihomo external
   controller, and kills idle keep-alive connections after a rotate. State in a
   local `register.json`. Also runs a background idle-connection tracker.
2. **mihomo architecture** — `app/config.yaml` (external-controller, N
   `select` proxy-groups + N `mixed` listeners, `rules: MATCH,DIRECT`),
   `app/gen-provider.py` (reads hub publish files → clean filter → writes
   `providers/hub-nodes.yaml` → hot-reloads `/providers/proxies/hub`), systemd
   unit examples.
3. **vpngate-hub.py** (~2544 lines): fetches node lists from several sources,
   merges/probes/ranks, writes local publish JSON (`hub-nodes.json`,
   `hub-gpcfg.json`), optionally pushes to a Cloudflare KV (token in an env file).
4. **vpngate-speed.py** (~1535 lines): long-lived daemon; measures each node's
   throughput via a single mihomo + Cloudflare speed endpoint, refreshes a
   "quality" profile from several enrichment sources, writes `sp_ewma` and risk
   into the hub SQLite.
5. **provider filter chain** (from ops docs): hub publish has NO risk filter;
   `gen-provider.py clean_nodes()` applies `r`/`f` flags + `risk<=50` + IP dedup.
   Veto nodes lack `r`/`f` and are dropped there. Keep this chain intact.

## 4. Source legality inventory (the sensitive part)

### vpngate-hub `SOURCE_ORDER`

| source | endpoint | nature | verdict |
|---|---|---|---|
| `vpngate_csv` | official public CSV + base64 .ovpn endpoint | official VPNGate public API (~100 nodes) | **INCLUDE** (the "官方 100 節點清單") |
| `vpngate_official` | official client data-file endpoint, RC4/SHA1-obfuscated payload | official VPNGate domain, reverse-engineered wire format (~200–300 nodes) | **INCLUDE**, documented as reverse-engineered-but-official (user: 灰色但理論上合法) |
| `giamping` | third-party Android-app PHP endpoint; hardcoded AES key/IV/adsid; decrypted `serversse` payload | access key/IV **extracted from a decompiled app binary** | **EXCLUDE** (user: 拆包解的憑證不能包) |
| `giamping_gp` | companion `serversgp` field, `*.giamping.com` GP hosts, shared CA + `userpassgp` credentials | same third-party source, ships extracted credentials | **EXCLUDE**, drop the whole GP node pipeline |
| `inbox` | local `*.txt` manual drops | operator's own local input | **INCLUDE** as a generic local-input mechanism (path → example/env) |

Excluding giamping also removes: `GIAMPING_*` constants, `_aes_cbc_decrypt`,
`_giamping_message`, `parse_gp_record`, `parse_gp_config`, `fetch_giamping`, the
GP host regexes/suffix, the `serversgp`/`hub-gpcfg.json` publish path, and the GP
branch in `gen-provider.py`. The two official fetchers (`fetch_vpngate_csv`,
`fetch_vpngate_official` via `_rc4`) stay.

### vpngate-speed enrichment sources

| source | nature | verdict |
|---|---|---|
| Cloudflare `__down` speed + warm endpoints | public speed-test URL | INCLUDE |
| `ip-api.com/batch` (no key) | free GeoIP | INCLUDE |
| `X4BNet/lists_vpn` GitHub raw CIDR lists | public open-source lists | INCLUDE |
| operator "peer block" worker `/api/peer?ip=` | serves the exact mihomo proxy block for a node; private operator endpoint | INCLUDE but **parameterize** the URL into config/example (no baked host) |
| `proxycheck.io` (key) + `scamalytics` (key, HTML-scraped legacy) | commercial keyed enrichment / scraped HTML | **OPTIONAL, example-only**: ship the call shape behind an `.example` env with placeholders, no keys, no scraped scores. `scrub_scam()` already purges legacy scraped scores — keep that. |

### Hard rule (user: 不含憑證，只包服務架構)

No credentials ship. All tokens/keys/certs/state/paths/hosts move to `*.example`
with placeholders and are gitignored. This includes the CF KV token env, the
OpenVPN CA/cert/key template embedded in `gen-provider.py` (public upstream
material, but externalize to a gitignored template + `.example`), and `register.json`.

## 5. Findings / open questions for the plan

- **F1 (windscribe drift):** upstream GitHub `main` has no web UI; the deployed
  host copy does. "Wrap unchanged" (submodule) would leave windscribe with no
  dashboard in the unified shell. The plan must decide: vendor the deployed UI
  into the collection, or have the unified shell render windscribe from
  `/status`/`/nodes`. Evidence: `git log --all -S'UI_HTML'` empty on main.
- **F2 (wrap mechanism):** git submodules pointing at the 3 upstream repos
  (truly untouched, independent updates) vs plain vendored copies (self-contained,
  reviewable). Decide from evidence; both keep the 3 pools functionally unchanged.
- **F3 (embedding):** reverse-proxy path prefixes + iframe vs a single shell that
  fetches each `/ui-state` and renders unified cards. The user asked to integrate
  the four *existing* web UIs behind left-sidebar tabs in one URL; only
  shared-mihomo needs a *new* UI.
- **F4 (new shared-mihomo UI):** no quota (VPNGate has no quota concept). Cards
  show uid / port / current node. Replace a country/node dropdown with ONE
  **Rotate** button per card, like urn-pool, calling `/rotate?uid=`.
- **F5 (CF KV push):** shared-mihomo consumes the *local* publish files, so the
  Cloudflare KV push path is not needed for the pool to function and carries a
  token dependency. Decide whether to keep it as optional/example or drop it.
- **F6 (speed peer URL):** if F5 drops the worker, speed daemon must build proxy
  blocks locally from `hub-nodes.json` instead of `/api/peer`. Keep them consistent.

## 6. Environment facts

- `gh` authenticated as `anton1615`; three upstream repos are public and readable.
- Target repo `anton1615/proxy-pool-collection` created, `origin` set, git identity
  is the noreply identity. No `.compound-engineering/config.yaml` → CE artifact
  root defaults to `docs/`.
- Tooling: `python3`, `node`, `playwright` (Python) available; no system chromium
  binary, but Playwright ships one.
- Three upstream clones at `/tmp/lfg-recon/{foxyvpn-pool,urn-pool,windscribe-pool}`;
  `register.py` fetched to `/tmp/lfg-recon/register.py`; hub/speed/gen-provider
  source snapshots in the local ops-doc tree (outside this repo).

## 7. Subtree pins (recorded by U1)

| Subtree path | Upstream | Pinned HEAD SHA |
|---|---|---|
| `pools/foxyvpn-pool/` | `anton1615/foxyvpn-pool` | `a39c5ec8beb673750b18e819ea58cc6d50d534e1` |
| `pools/urn-pool/` | `anton1615/urn-pool` | `2b75106bf7d8755d59b84332131ef1a0d89df21c` |
| `pools/windscribe-pool/` | `anton1615/windscribe-pool` | `e5acc237cb4e3cd5a9dbd9037594c7a2d84835ed` |

These are the verified HEADs read this run via `git ls-remote`; the subtree-integrity
check diffs the vendored dirs against exactly these commits.
