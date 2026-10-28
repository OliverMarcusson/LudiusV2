#!/usr/bin/env bash
# Exposure check for the monitor on the box. Fails closed: a tool that cannot answer (ss, ip, tailscale, jq) aborts
# with exit 2, a failed check exits 1, only a clean pass exits 0.
#   1. no listener on a monitor port except on loopback (IPv4 and IPv6), read from `ss`
#   2. no connect succeeds to a monitor port on any global address of the box (IPv4 and IPv6), tailscale0 excluded
#   3. `tailscale serve status --json` holds exactly one mapping, :TS_PORT -> AGENT_MONITOR, and no funnel
# `tailscale serve` listens inside tailscaled, so `ss` never shows the tailnet port; check 2 covers it.
#   infra/monitor/check.sh     (AGENT_MONITOR, TS_PORT, PROBE_TIMEOUT override the defaults)
set -euo pipefail

: "${AGENT_MONITOR:=http://127.0.0.1:7452}"
: "${TS_PORT:=8452}"
: "${PROBE_TIMEOUT:=3}"
agent_port=${AGENT_MONITOR##*:}
# 7452 and 7453 (the Host-fixing fallback proxy) are always checked, whichever one serve points at
ports=$(printf '%s\n' 7452 7453 "$agent_port" "$TS_PORT" | sort -u)
fail=0
bad() { echo "FAIL: $*"; fail=1; }
ok() { echo "ok:   $*"; }
die() { echo "ABORT: $*" >&2; exit 2; }

command -v jq >/dev/null || die "jq is missing (apt-get install jq)"

# 1. listeners. A failed or empty `ss` is not "no listeners": there is always at least sshd.
ss_out=$(ss -H -ltn) || die "ss failed, cannot list listeners"
[[ -n $ss_out ]] || die "ss listed no listeners at all"
agent_up=
while read -r addr; do
  [[ $addr =~ ^(.+):([0-9]+)$ ]] || die "unparsable listener address: $addr"
  host=${BASH_REMATCH[1]}; port=${BASH_REMATCH[2]}
  grep -qx "$port" <<<"$ports" || continue
  case $host in
    127.* | '[::1]' | '[::ffff:127.'*) ;;
    100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].* | '[fd7a:115c:a1e0:'*) ;; # tailnet
    *) bad "port $port listens on $host (must be loopback)"; continue ;;
  esac
  [[ $addr == "${AGENT_MONITOR#http://}" ]] && agent_up=1
done < <(awk '{print $4}' <<<"$ss_out")
if [[ -n $agent_up ]]; then ok "agent monitor on ${AGENT_MONITOR#http://}, no wider listener on $(tr '\n' ' ' <<<"$ports")"
else echo "warn: nothing on ${AGENT_MONITOR#http://} yet (agent not running?)"; fi

# 2. connect probes to every global address (a connect to our own address reaches any 0.0.0.0 or :: listener).
# Only a refused or timed-out connect passes.
addrs=$(ip -o addr show scope global) || die "ip addr failed, cannot find the box's addresses"
probed=0
while read -r _ dev _ cidr _; do
  [[ $dev == tailscale0 || -z ${cidr:-} ]] && continue
  a=${cidr%/*}
  probed=$((probed + 1))
  for p in $ports; do
    # shellcheck disable=SC2016 # $1 and $2 are for the inner bash
    if timeout "$PROBE_TIMEOUT" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$a" "$p" 2>/dev/null; then bad "$a:$p answers"
    else ok "$a:$p refuses"; fi
  done
done <<<"$addrs"
[[ $probed -gt 0 ]] || bad "no global address found to probe"

# 3. serve config: only :TS_PORT, only the one proxy to AGENT_MONITOR, no funnel, no Services
serve_json=$(tailscale serve status --json) || die "tailscale serve status failed"
if jq -e --arg port "$TS_PORT" --arg target "$AGENT_MONITOR" '
     ((.TCP // {}) | keys) == [$port]
     and ((.Web // {}) | length) >= 1
     and ((.Web // {}) | keys | all(endswith(":" + $port)))
     and ([(.Web // {})[].Handlers // {} | to_entries[]] | length >= 1 and all(.key == "/" and .value.Proxy == $target))
     and (((.AllowFunnel // {}) | [.[]] | any) | not)
     and ((.Services // {}) | length == 0)' <<<"$serve_json" >/dev/null; then
  ok "tailscale serve holds only :$TS_PORT -> $AGENT_MONITOR, no funnel"
else
  bad "tailscale serve config is not exactly :$TS_PORT -> $AGENT_MONITOR: $(jq -c . <<<"$serve_json" 2>&1)"
fi

exit "$fail"
