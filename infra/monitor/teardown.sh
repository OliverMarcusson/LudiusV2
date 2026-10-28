#!/usr/bin/env bash
# Box side of the end-of-rental teardown: drop the serve mapping and log the box out of the tailnet.
# The homelab side (Caddy block, DDNS domain, DNS record) is in README.md. DRY_RUN=1 prints the commands.
set -euo pipefail

: "${TS_PORT:=8452}"
run() { if [[ -n ${DRY_RUN:-} ]]; then printf '+ %s\n' "$*"; else "$@"; fi; }

run tailscale serve --http="$TS_PORT" off
run tailscale logout
echo "now delete the machine in the Tailscale admin console if it is still listed, and revoke the auth key"
