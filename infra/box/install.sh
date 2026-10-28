#!/usr/bin/env bash
# Install or update the host agent on the Proxmox box: /opt/ludius-agent, systemd unit `ludius-agent`, and the small
# Caddy (127.0.0.1:7453) that fixes Host for the monitor. Run as root on the box from a checkout of this repo, with
# the agent published (no dotnet needed on the box) and the monitor UI built (infra/monitor/bring-up.sh does that):
#   desktop:  dotnet publish host/agent -f net10.0 -r linux-x64 --self-contained -c Release -o out/agent-linux-x64
#             then copy out/agent-linux-x64 to <checkout>/out/agent-linux-x64 on the box
#   box:      infra/box/install.sh [publish dir]
# Re-runnable. Does not touch tailscale, pveproxy or the firewall; switching `serve` to :7453 is
#   AGENT_MONITOR=http://127.0.0.1:7453 infra/monitor/bring-up.sh
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
pub=${1:-$repo/out/agent-linux-x64}
dist=$repo/web/monitor/dist
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f $pub/ludius-host-agent ]] || { echo "no published agent in $pub" >&2; exit 1; }
[[ -f $dist/index.html ]] || { echo "monitor UI not built in $dist (infra/monitor/bring-up.sh)" >&2; exit 1; }

if ! command -v caddy >/dev/null; then
  # The package starts a default site on :80 at install; policy-rc.d keeps it stopped until our Caddyfile is in.
  printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d
  trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  apt-get install -y caddy
  rm -f /usr/sbin/policy-rc.d
fi
install -m 0644 "$repo/infra/box/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

systemctl stop ludius-agent 2>/dev/null || true
mkdir -p /opt/ludius-agent
cp -a "$pub"/. /opt/ludius-agent/
chmod 0755 /opt/ludius-agent/ludius-host-agent
rm -rf /opt/ludius-agent/monitor && cp -a "$dist" /opt/ludius-agent/monitor
install -m 0644 "$repo/infra/box/ludius-agent.service" /etc/systemd/system/ludius-agent.service

systemctl daemon-reload
systemctl enable ludius-agent caddy
systemctl restart caddy
systemctl start ludius-agent
