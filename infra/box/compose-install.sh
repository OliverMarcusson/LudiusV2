#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

apt-get install -y docker-compose aardvark-dns
systemctl enable --now podman.socket

install -d -m 0755 /etc/ludius
rm -rf /etc/ludius/compose && cp -a "$here/../compose" /etc/ludius/compose
if [[ ! -f /etc/ludius/compose.env ]]; then
  umask 077
  printf '%s\n' "TEMPORAL_DB_PASSWORD=$(openssl rand -hex 16)" "LUDIUS_DB_PASSWORD=$(openssl rand -hex 16)" >/etc/ludius/compose.env
fi

if [[ -f /etc/containers/systemd/ludius-postgres.container ]]; then
  systemctl stop ludius-temporal-namespace ludius-temporal ludius-temporal-setup ludius-postgres || true
  rm -f /etc/containers/systemd/ludius-{postgres,temporal,temporal-setup,temporal-namespace}.container /etc/containers/systemd/ludius-pgdata.volume
  systemctl daemon-reload
  if podman volume exists ludius-pgdata && ! podman volume exists ludius_pgdata; then
    podman volume create ludius_pgdata
    cp -a "$(podman volume inspect ludius-pgdata --format '{{.Mountpoint}}')"/. "$(podman volume inspect ludius_pgdata --format '{{.Mountpoint}}')"/
  fi
fi

install -m 0644 "$here/ludius-compose.service" /etc/systemd/system/ludius-compose.service
systemctl daemon-reload
systemctl enable ludius-compose.service
systemctl restart ludius-compose.service
