#!/usr/bin/env bash
# Exposure check for the monitor on the box: the agent's monitor port must stay on loopback and nothing may answer
# for it on the public address. `tailscale serve` listens inside tailscaled (netstack), so `ss` never shows the
# tailnet port; the probes against the public address cover it.
#   infra/monitor/check.sh     (exit 0 = ok, 1 = a check failed)
set -euo pipefail

: "${AGENT_PORT:=7452}"
: "${TS_PORT:=8452}"
fail=0
bad() { echo "FAIL: $*"; fail=1; }
ok() { echo "ok:   $*"; }

# 1. every kernel listener on the monitor ports is loopback (or a tailnet address)
agent_up=
while read -r addr; do
  host=${addr%:*}; port=${addr##*:}
  [[ $port == "$AGENT_PORT" || $port == "$TS_PORT" ]] || continue
  case $host in
    127.* | '[::1]' | 100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].* | '[fd7a:115c:a1e0:'*) ;;
    *) bad "port $port listens on $host (must be 127.0.0.1)"; continue ;;
  esac
  [[ $addr == "127.0.0.1:$AGENT_PORT" ]] && agent_up=1
done < <(ss -H -ltn | awk '{print $4}')
[[ -n $agent_up ]] && ok "agent monitor on 127.0.0.1:$AGENT_PORT, no wider listener" \
  || echo "warn: nothing on 127.0.0.1:$AGENT_PORT yet (agent not running?)"

# 2. nothing answers on the public address (a connect to our own public IP reaches any 0.0.0.0 listener)
pub=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
if [[ -z $pub ]]; then bad "no public address found"; else
  for p in "$AGENT_PORT" "$TS_PORT"; do
    if curl -s -o /dev/null --max-time 3 "http://$pub:$p/"; then bad "http://$pub:$p answers on the public address"
    else ok "http://$pub:$p refuses"; fi
  done
fi

# 3. the serve mapping is the one we expect, and it is tailnet-only (not funnel)
status=$(tailscale serve status 2>&1 || true)
if grep -q ":$TS_PORT" <<<"$status" && grep -q "127.0.0.1:$AGENT_PORT" <<<"$status"; then
  ok "tailscale serve maps :$TS_PORT -> 127.0.0.1:$AGENT_PORT"
else
  bad "tailscale serve status does not show the mapping: $status"
fi
grep -qi 'funnel on' <<<"$status" && bad "funnel is on"

exit "$fail"
