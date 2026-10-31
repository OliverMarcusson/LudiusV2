#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
# The host agent and the monitor run from infra/compose (compose-install.sh); this installs the Caddy and the timer.

ln -sf /dev/null /etc/systemd/system/caddy.service
systemctl daemon-reload
if ! command -v caddy >/dev/null; then
  apt-get install -y caddy
fi
install -m 0644 "$repo/infra/box/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
rm -f /etc/systemd/system/caddy.service

systemctl disable --now ludius-agent 2>/dev/null || true
for u in ludius-clock-sweep.service ludius-clock-sweep.timer; do install -m 0644 "$repo/infra/box/$u" /etc/systemd/system/$u; done

systemctl daemon-reload
systemctl enable caddy
systemctl enable --now ludius-clock-sweep.timer
systemctl restart caddy
