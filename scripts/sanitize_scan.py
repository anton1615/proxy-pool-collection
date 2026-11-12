#!/usr/bin/env python3
"""Scan first-party executable files for credentials / private-infra literals.

The three vendored upstream subtrees under pools/ are byte-identical public
content. They are verified by the subtree diff, not by this scanner; only the
documented allowlist literals listed in docs/RECON.md are tolerated there.

Docs and tests are prose that describe the very symbols/values the scanner
looks for, and the scanner's own rule table would always match itself, so
only shipped executable/config surface is scanned.

No real key bytes appear in this file. The removed-source symbols are checked
by name only.

Usage: python3 scripts/sanitize_scan.py [root]   (root defaults to repo root)
Exit 0 when zero unallowed hits, 1 otherwise.
"""
import os
import re
import sys

FIRST_PARTY_TOP = {"webui", "shared-mihomo", "windscribe-host-webui", "scripts"}
FIRST_PARTY_FILES = {"README.md", "LICENSE", ".gitignore"}
SKIP_TOP = {"docs", "tests"}
SKIP_FILES = {"sanitize_scan.py"}
VENDOR_PREFIX = "pools/"

RULES = [
    ("private-ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("tailnet", re.compile(r"\b[a-z0-9-]+\.ts\.net\b", re.I)),
    ("token-prefix", re.compile(r"\b(?:tskey-|ibk_|sk-[A-Za-z0-9]{20,})")),
    ("opaque-token", re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{40}(?![A-Za-z0-9])")),
    ("pem", re.compile(r"-----BEGIN (?:CERTIFICATE|PRIVATE KEY|RSA PRIVATE KEY|OPENSSH PRIVATE KEY)-----")),
    ("removed-source-symbol", re.compile(
        r"\b(?:GIAMPING_|_aes_cbc_decrypt|_giamping_message|parse_gp_record|"
        r"parse_gp_config|adsid|serversse|serversgp|hub-gpcfg|"
        r"proxycheck|scamalytics)\b")),
    ("internal-host", re.compile(r"\b(?:sesame-1|oracle-tailscale|instabox-\d|sprite@|ubuntu@)\b", re.I)),
]

# Literals that legitimately appear in the pinned public upstream trees.
# Keep this list in sync with docs/RECON.md; revisit on every SHA bump.
UPSTREAM_ALLOWLIST = re.compile(
    r"100\.100\.100\.101|100\.64\.0\.0/10|pool-host\.example\.ts\.net|"
    r'"seedphrase"|"password"|Environment=PUBLIC_HOST='
)


def is_first_party(rel):
    if rel.startswith(VENDOR_PREFIX):
        return False
    parts = rel.split(os.sep)
    return parts[0] in FIRST_PARTY_TOP or rel in FIRST_PARTY_FILES


def scan(root):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "__pycache__", ".venv", "node_modules"}]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            parts = rel.split(os.sep)
            if (rel.startswith(".git/") or os.path.islink(full)
                    or parts[0] in SKIP_TOP or fn in SKIP_FILES):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            first_party = is_first_party(rel)
            for label, rx in RULES:
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    snippet = m.group(0)[:60]
                    if not first_party:
                        # Vendored trees: allow only the documented literals,
                        # and never a removed-source symbol or a PEM block.
                        if label in {"removed-source-symbol", "pem", "token-prefix"}:
                            hits.append((rel, line, label, snippet))
                        elif not UPSTREAM_ALLOWLIST.search(
                                text[max(0, m.start() - 40):m.end() + 40]):
                            hits.append((rel, line, label, snippet))
                    else:
                        hits.append((rel, line, label, snippet))
    return hits


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    hits = scan(root)
    for rel, line, label, snippet in hits:
        print(f"{rel}:{line}: {label}: {snippet!r}", file=sys.stderr)
    if hits:
        print(f"sanitize_scan: {len(hits)} unallowed hit(s)", file=sys.stderr)
        return 1
    print("sanitize_scan: clean (first-party zero hits; vendored trees allowlisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
