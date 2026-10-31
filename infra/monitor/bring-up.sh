#!/usr/bin/env bash
set -euo pipefail

: "${TS_HOSTNAME:=ludius-box}"
: "${TS_TAG:=tag:ludius-box}"
: "${TS_PORT:=8452}"
: "${AGENT_MONITOR:=http://127.0.0.1:7452}"
: "${PVE_TS_PORT:=8453}"
: "${PVE_TARGET:=https+insecure://127.0.0.1:8006}"
: "${BUN_VERSION:=1.3.14}"
PVEPROXY_DEFAULT=${PVEPROXY_DEFAULT:-/etc/default/pveproxy}
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
  cd "$repo/web/console"
  bun install --frozen-lockfile
  bun run build
}

verify_identity() {
  local st prefs
  st=$(tailscale status --json) || { echo "cannot read tailscale status" >&2; return 1; }
  prefs=$(tailscale debug prefs) || { echo "cannot read tailscale prefs" >&2; return 1; }
  if ! jq -e --arg tag "$TS_TAG" '
      (.Self.Tags | type) == "array" and .Self.Tags == [$tag]
      and (.Self.UserID | type) == "number"
      and (.User | type) == "object"
      and (.User[(.Self.UserID | tostring)] | type) == "object"
      and (.User[(.Self.UserID | tostring)].LoginName | type) == "string"
      and (.User[(.Self.UserID | tostring)].LoginName as $l
           | $l == "tagged-devices" or ($l | test("^[a-z0-9-]+\\.[a-z0-9-]+\\.ts\\.net$")))' <<<"$st" >/dev/null \
    || ! jq -e '
      has("AdvertiseRoutes") and ((.AdvertiseRoutes | type) == "null" or (.AdvertiseRoutes | type) == "array")
      and (.AdvertiseRoutes // [] | length) == 0
      and (.ExitNodeID | type) == "string" and .ExitNodeID == ""
      and (.ExitNodeIP | type) == "string" and .ExitNodeIP == ""' <<<"$prefs" >/dev/null; then
    echo "ABORT: this box is already on a tailnet with an unexpected identity (need only $TS_TAG, no user owner, no routes or exit node, and every field readable):" >&2
    jq -c '{Tags: .Self.Tags, UserID: .Self.UserID, HostName: .Self.HostName}' <<<"$st" >&2 || true
    jq -c '{AdvertiseRoutes, ExitNodeID, ExitNodeIP}' <<<"$prefs" >&2 || true
    echo "Run 'tailscale logout' on the box, then run this script again with TS_AUTHKEY." >&2
    return 1
  fi
  echo "tailscale identity ok: only $TS_TAG, no routes, no exit node"
}

if [[ -z $DRY_RUN && $EUID -ne 0 ]]; then echo "run as root" >&2; exit 1; fi

run apt-get install -y curl gnupg ca-certificates unzip jq
command -v tailscale >/dev/null || run install_tailscale

if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
  echo "tailscale already up as $(tailscale ip -4 | head -1)"
  verify_identity
else
  : "${TS_AUTHKEY:?set TS_AUTHKEY to a tagged, pre-authorised (ideally ephemeral) auth key}"
  if [[ -n $DRY_RUN ]]; then
    echo "+ tailscale up --auth-key=<TS_AUTHKEY> --hostname=$TS_HOSTNAME --advertise-tags=$TS_TAG --accept-dns=false --accept-routes=false"
    echo "+ verify_identity"
  else
    tailscale up --auth-key="$TS_AUTHKEY" --hostname="$TS_HOSTNAME" --advertise-tags="$TS_TAG" \
      --accept-dns=false --accept-routes=false
    verify_identity
  fi
fi

bind_pve_loopback() {
  local changed=
  grep -qx 'LISTEN_IP="127.0.0.1"' "$PVEPROXY_DEFAULT" 2>/dev/null || {
    printf '%s\n' '# Managed by infra/monitor/bring-up.sh. Reached over the tailnet via tailscale serve.' 'LISTEN_IP="127.0.0.1"' >"$PVEPROXY_DEFAULT"
    changed=1
  }
  [[ -z $changed ]] || systemctl restart pveproxy
  local u
  for u in spiceproxy.service rpcbind.service rpcbind.socket; do
    if systemctl cat "$u" >/dev/null 2>&1; then systemctl mask --now "$u"; fi
  done
}
run bind_pve_loopback

run tailscale serve --bg --http="$TS_PORT" "$AGENT_MONITOR"
run tailscale serve --bg --http="$PVE_TS_PORT" "$PVE_TARGET"

command -v bun >/dev/null || run install_bun
run build_monitor

echo "monitor UI built: $repo/web/console/dist"
echo "agent needs: Monitor__Root=$repo/web/console/dist  Monitor__AllowedOrigins=https://monitor.ludius.se"
[[ -n $DRY_RUN ]] || TS_PORT=$TS_PORT AGENT_MONITOR=$AGENT_MONITOR PVE_TS_PORT=$PVE_TS_PORT PVE_TARGET=$PVE_TARGET "$repo/infra/monitor/check.sh"
