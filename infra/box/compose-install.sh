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

# host agent secrets (range setup fills them): pve_token is USER@REALM!TOKENID=SECRET; pve_root_password
# (root@pam, only for setting a clone's vsock args) may stay empty; openrouter_key (the monitor's portraits) too.
install -d -m 0700 /etc/ludius/secrets /var/lib/ludius/agent-scratch
for f in pve_token pve_root_password openrouter_key; do [[ -f /etc/ludius/secrets/$f ]] || install -m 0600 /dev/null /etc/ludius/secrets/$f; done
grep -q '^LUDIUS_SECRETS=' /etc/ludius/compose.env || echo LUDIUS_SECRETS=/etc/ludius/secrets >>/etc/ludius/compose.env
install -m 0644 "$here/ludius-compose.service" /etc/systemd/system/ludius-compose.service
systemctl daemon-reload
systemctl enable ludius-compose.service
systemctl restart ludius-compose.service
