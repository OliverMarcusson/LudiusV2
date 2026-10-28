#!/usr/bin/env bash
# Temporal + PostgreSQL on the Proxmox box with Podman (Quadlet units), the same stack as infra/compose/compose.yaml.
# Host networking with loopback binds: Podman adds no bridge and no firewall rules; `ss -tlnp` shows 127.0.0.1 only.
# Run as root on the box from a checkout. Re-runnable; passwords are generated once into /etc/ludius/compose.env (0600)
# and never printed. Data lives in the named volume ludius-pgdata (survives restarts and reboots).
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
compose=$here/../../compose
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

install -d -m 0755 /etc/ludius /etc/ludius/initdb /etc/ludius/dynamicconfig  # containers read these mounts; compose.env is 0600
install -m 0644 "$compose/initdb/10-ludius.sh" /etc/ludius/initdb/10-ludius.sh
install -m 0644 "$compose/dynamicconfig/docker.yaml" /etc/ludius/dynamicconfig/docker.yaml
install -m 0644 "$here/setup.sh" "$here/namespace.sh" /etc/ludius/
if [[ ! -f /etc/ludius/compose.env ]]; then
  umask 077
  t=$(openssl rand -hex 16) l=$(openssl rand -hex 16)
  printf '%s\n' "TEMPORAL_DB_PASSWORD=$t" "POSTGRES_PASSWORD=$t" "SQL_PASSWORD=$t" "POSTGRES_PWD=$t" \
    "LUDIUS_DB_PASSWORD=$l" >/etc/ludius/compose.env
fi
install -d /etc/containers/systemd
install -m 0644 "$here"/*.container "$here"/*.volume /etc/containers/systemd/
systemctl daemon-reload
systemctl start ludius-temporal-namespace.service   # pulls in postgres -> schema -> temporal
