#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
compose=$here/../../compose
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

install -d -m 0755 /etc/ludius /etc/ludius/initdb /etc/ludius/dynamicconfig
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
systemctl start ludius-temporal-namespace.service
