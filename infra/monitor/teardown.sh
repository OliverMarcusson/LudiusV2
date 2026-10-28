#!/usr/bin/env bash
# Box side of the end-of-rental teardown: drop both serve mappings and log the box out of the tailnet. pveproxy stays
# on loopback (the box goes back to the host provider anyway).
# The homelab side (Caddy block, DDNS domain, DNS record) is in README.md. DRY_RUN=1 prints the commands.
set -euo pipefail

: "${TS_PORT:=8452}"
: "${PVE_TS_PORT:=8453}"
run() { if [[ -n ${DRY_RUN:-} ]]; then printf '+ %s\n' "$*"; else "$@"; fi; }

# a mapping that is already gone must not stop the logout
run tailscale serve --http="$TS_PORT" off || echo "warn: no serve mapping on :$TS_PORT"
run tailscale serve --http="$PVE_TS_PORT" off || echo "warn: no serve mapping on :$PVE_TS_PORT"
run tailscale logout
echo "now delete the machine in the Tailscale admin console if it is still listed, and revoke the auth key"
