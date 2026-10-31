#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
pub=${1:-$repo/out/agent-linux-x64}
dist=$repo/web/monitor/dist
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f $pub/ludius-host-agent ]] || { echo "no published agent in $pub" >&2; exit 1; }
[[ -f $dist/index.html ]] || { echo "monitor UI not built in $dist (infra/monitor/bring-up.sh)" >&2; exit 1; }

ln -sf /dev/null /etc/systemd/system/caddy.service
systemctl daemon-reload
if ! command -v caddy >/dev/null; then
  apt-get install -y caddy
fi
install -m 0644 "$repo/infra/box/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
rm -f /etc/systemd/system/caddy.service

systemctl stop ludius-agent 2>/dev/null || true
mkdir -p /opt/ludius-agent
cp -a "$pub"/. /opt/ludius-agent/
chmod 0755 /opt/ludius-agent/ludius-host-agent
rm -rf /opt/ludius-agent/monitor && cp -a "$dist" /opt/ludius-agent/monitor
install -m 0644 "$repo/infra/box/ludius-agent.service" /etc/systemd/system/ludius-agent.service
for u in ludius-clock-sweep.service ludius-clock-sweep.timer; do install -m 0644 "$repo/infra/box/$u" /etc/systemd/system/$u; done

systemctl daemon-reload
systemctl enable ludius-agent caddy
systemctl enable --now ludius-clock-sweep.timer
systemctl restart caddy
systemctl start ludius-agent
