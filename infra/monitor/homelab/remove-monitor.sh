#!/usr/bin/env bash
# Removes a marked block (ludius-monitor, or ludius-pve with BLOCK=ludius-pve) from the edge Caddyfile (run on the homelab, after the owner's OK).
# It touches the file only if there is exactly one BEGIN and one END marker line, BEGIN first. It backs up first,
# validates the result with Caddy, puts the backup back if validation fails, and reloads only after validation.
#   CADDY='docker compose -f /srv/compose/edge/compose.yaml exec -T <caddy-service> caddy' \
#     infra/monitor/homelab/remove-monitor.sh [/srv/compose/edge/Caddyfile]
# CADDY is the command prefix that runs `caddy` where the Caddyfile is mounted; CADDY_CONF is the Caddyfile path
# in that environment (default /etc/caddy/Caddyfile). BLOCK names the marker pair (default ludius-monitor). DRY_RUN=1 checks the markers and prints, changes nothing.
set -euo pipefail

file=${1:-/srv/compose/edge/Caddyfile}
: "${CADDY:?set CADDY to the command that runs caddy, e.g. 'docker compose exec -T <service> caddy'}"
: "${CADDY_CONF:=/etc/caddy/Caddyfile}"
: "${BLOCK:=ludius-monitor}"
[[ $BLOCK =~ ^[a-z][a-z0-9-]*$ ]] || { echo "ABORT: bad BLOCK name: $BLOCK" >&2; exit 1; }
backup=$file.bak-before-$BLOCK-teardown
begin="# BEGIN $BLOCK"
end="# END $BLOCK"
read -ra caddy <<<"$CADDY"

[[ -f $file ]] || { echo "ABORT: $file not found" >&2; exit 1; }
nb=$(grep -c -x -F "$begin" "$file" || true)
ne=$(grep -c -x -F "$end" "$file" || true)
if [[ $nb -ne 1 || $ne -ne 1 ]]; then
  echo "ABORT: need exactly one '$begin' and one '$end' line, found $nb and $ne. $file untouched." >&2
  exit 1
fi
lb=$(grep -n -x -F "$begin" "$file" | cut -d: -f1)
le=$(grep -n -x -F "$end" "$file" | cut -d: -f1)
if [[ $lb -ge $le ]]; then
  echo "ABORT: '$end' (line $le) is not after '$begin' (line $lb). $file untouched." >&2
  exit 1
fi
echo "removing lines $lb-$le of $file"
if [[ -n ${DRY_RUN:-} ]]; then sed -n "${lb},${le}p" "$file"; exit 0; fi

# A leftover backup is fine only if it is identical to the current file (an earlier run that failed validation).
if [[ -e $backup ]] && ! cmp -s "$backup" "$file"; then
  echo "ABORT: $backup exists and differs from $file; move it aside first." >&2
  exit 1
fi
[[ -e $backup ]] || cp -a "$file" "$backup"

# Write in place (not mv): a single-file bind mount keeps the old inode and would not see a renamed file.
sed "${lb},${le}d" "$file" >"$file.new"
cat "$file.new" >"$file"
rm -f "$file.new"

if ! "${caddy[@]}" validate --config "$CADDY_CONF"; then
  cat "$backup" >"$file"
  echo "ABORT: caddy validate failed; $file restored from $backup, nothing reloaded." >&2
  exit 1
fi
"${caddy[@]}" reload --config "$CADDY_CONF"
echo "removed; backup at $backup"
