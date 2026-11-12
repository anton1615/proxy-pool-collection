# Proxy Pool Collection

Four self-hosted proxy pools, one URL.

An operator opens a single page, switches between **FoxyVPN**, **URnetwork**,
**Windscribe**, and **VPNGate / shared-mihomo** with the left sidebar, and sees
each pool's own dashboard. This repo is the **service architecture only**: it
contains no credentials, tokens, keys, accounts, or runtime state.

## What is inside

| Tab | Source | Web UI |
|---|---|---|
| FoxyVPN | `pools/foxyvpn-pool/` (upstream subtree, pinned) | upstream embedded `UI_HTML` |
| URnetwork | `pools/urn-pool/` (upstream subtree, pinned) | upstream embedded `UI_HTML` |
| Windscribe | `pools/windscribe-pool/` (upstream subtree, pinned) | `windscribe-host-webui/` deployed host snapshot |
| VPNGate / shared-mihomo | `shared-mihomo/` (first-party) | `shared-mihomo/webui/ui.html` |

The three upstream pools are vendored **byte-identical** as git subtrees pinned
to the SHAs in `docs/RECON.md`. The fourth pool's HTTP API, mihomo config,
provider generator, VPNGate hub and speed daemon are first-party, sanitized to
use only the official public VPNGate node lists.

`webui/server.py` is a stdlib reverse proxy: `/` serves the tab shell, and each
`/p/<pool>/` prefix proxies to that pool's configured upstream. The VPNGate
dashboard is served directly at `/p/vg/`.

## Quickstart

```bash
cp webui/config.example.json webui/config.json
# edit webui/config.json: set each pool's upstream base URL + ui_path,
# and the listen/port (defaults to 127.0.0.1).
python3 -m unittest discover -s tests -v
python3 webui/server.py
```

Open the printed URL. With no pool running, a tab shows an error panel and the
other tabs keep working. The shell defaults to loopback; binding it to a tailnet
address is an explicit opt-in in `config.json`.

The fourth pool's services each ship a `config.example.env` and a
`.service.example` under `shared-mihomo/{api,mihomo,hub,speed}/`. Copy each
example to a gitignored runtime config, fill the placeholders, and start the
units. `shared-mihomo/mihomo/ovpn-template.example.json` is the placeholder
for the live `ovpn-template.json`.

Run the credential scan before publishing:

```bash
python3 scripts/sanitize_scan.py
```

Every runtime value has a `*.example` companion and is gitignored. The three
upstream subtrees are covered by the byte-identical diff and the documented
allowlist in `docs/RECON.md`, not by the first-party scanner.

## Source policy

- **Included:** the official VPNGate CSV endpoint and the official client
  data-file endpoint (documented as a reverse-engineered wire format on an
  official domain), plus a local inbox for operator-supplied hosts.
- **Excluded:** the third-party Android-app source whose access key/IV were
  extracted from a decompiled binary, its GP node pipeline and shared
  credentials, the Cloudflare KV push path and its token, and keyed/scraped
  risk-enrichment sources. No key bytes or scraped scores are present anywhere.
- The OpenVPN CA/cert/key template is externalized to
  `ovpn-template.example.json`; the live `ovpn-template.json` is gitignored.

## Credits

- [anton1615/foxyvpn-pool](https://github.com/anton1615/foxyvpn-pool) — MIT
- [anton1615/urn-pool](https://github.com/anton1615/urn-pool) — MIT
- [anton1615/windscribe-pool](https://github.com/anton1615/windscribe-pool) — MIT
- shared-mihomo, vpngate-hub, vpngate-speed, the unified web UI and the VPNGate
  dashboard are first-party components.

Unofficial community software; not affiliated with or endorsed by Mozilla,
URnetwork, Windscribe, or VPNGate. Use only with accounts and subscriptions
you are authorized to use.

See `docs/RECON.md` for the verified inventory and `docs/plans/` for the
implementation plan.
