#!/usr/bin/env bash
# Box side of the monitor: Tailscale (tagged key, tailnet only), `tailscale serve` for the agent's monitor
# endpoint, and the built monitor UI. Run as root on the Proxmox box from a checkout of this repo:
#   TS_AUTHKEY=tskey-auth-... infra/monitor/bring-up.sh
#   DRY_RUN=1 infra/monitor/bring-up.sh        # prints the mutating commands, changes nothing
# Re-runnable: each step is skipped or repeated harmlessly.
set -euo pipefail

: "${TS_HOSTNAME:=ludius-box}"
: "${TS_TAG:=tag:ludius-box}"
: "${TS_PORT:=8452}"                    # tailnet-side port; the agent itself stays on 127.0.0.1:7452
: "${AGENT_MONITOR:=http://127.0.0.1:7452}"
: "${BUN_VERSION:=1.3.14}"
DRY_RUN=${DRY_RUN:-}

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

run() { if [[ -n $DRY_RUN ]]; then printf '+ %s\n' "$*"; else "$@"; fi; }

install_tailscale() {
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
}

install_bun() {
  local tmp base
  tmp=$(mktemp -d)
  base=https://github.com/oven-sh/bun/releases/download/bun-v$BUN_VERSION
  curl -fsSL "$base/bun-linux-x64.zip" -o "$tmp/bun-linux-x64.zip"
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  (cd "$tmp" && grep 'bun-linux-x64.zip$' SHASUMS256.txt | sha256sum -c -)
  unzip -q -o "$tmp/bun-linux-x64.zip" -d "$tmp"
  install -m 0755 "$tmp/bun-linux-x64/bun" /usr/local/bin/bun
  rm -rf "$tmp"
}

build_monitor() {
  cd "$repo/web/monitor"
  bun install --frozen-lockfile
  bun run build
}

if [[ -z $DRY_RUN && $EUID -ne 0 ]]; then echo "run as root" >&2; exit 1; fi

run apt-get install -y curl gnupg ca-certificates unzip
command -v tailscale >/dev/null || run install_tailscale

if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
  echo "tailscale already up as $(tailscale ip -4 | head -1)"
else
  : "${TS_AUTHKEY:?set TS_AUTHKEY to a tagged, pre-authorised (ideally ephemeral) auth key}"
  # accept-dns=false: the box's resolver stays as Hetzner set it. No routes or exit node are advertised.
  # ponytail: the key is visible in `ps` for the second this takes; root-only rental box, so left as is.
  if [[ -n $DRY_RUN ]]; then
    echo "+ tailscale up --auth-key=<TS_AUTHKEY> --hostname=$TS_HOSTNAME --advertise-tags=$TS_TAG --accept-dns=false --accept-routes=false"
  else
    tailscale up --auth-key="$TS_AUTHKEY" --hostname="$TS_HOSTNAME" --advertise-tags="$TS_TAG" \
      --accept-dns=false --accept-routes=false
  fi
fi

# tailnet-side HTTP listener (inside tailscaled, tailnet addresses only) -> the agent's loopback monitor endpoint
run tailscale serve --bg --http="$TS_PORT" "$AGENT_MONITOR"

command -v bun >/dev/null || run install_bun
run build_monitor

echo "monitor UI built: $repo/web/monitor/dist"
echo "agent needs: Monitor__Root=$repo/web/monitor/dist  Monitor__AllowedOrigins=https://monitor.ludius.se"
[[ -n $DRY_RUN ]] || TS_PORT=$TS_PORT "$repo/infra/monitor/check.sh"
