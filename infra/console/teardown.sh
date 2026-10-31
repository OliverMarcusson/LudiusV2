#!/usr/bin/env bash
set -euo pipefail

: "${TS_PORT:=8452}"
: "${PVE_TS_PORT:=8453}"
run() { if [[ -n ${DRY_RUN:-} ]]; then printf '+ %s\n' "$*"; else "$@"; fi; }

run tailscale serve --http="$TS_PORT" off || echo "warn: no serve mapping on :$TS_PORT"
run tailscale serve --http="$PVE_TS_PORT" off || echo "warn: no serve mapping on :$PVE_TS_PORT"
run tailscale logout
echo "now delete the machine in the Tailscale admin console if it is still listed, and revoke the auth key"
