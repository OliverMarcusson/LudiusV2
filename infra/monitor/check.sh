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

for t in ss ip tailscale jq timeout bash awk sort grep tr; do
  command -v "$t" >/dev/null || die "$t is missing (apt-get install jq iproute2 coreutils ...)"
done

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
# Only a refused or timed-out connect passes; a probe that could not run is an abort, not a pass.
valid_ip() {
  local o
  if [[ $1 =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for o in "${BASH_REMATCH[@]:1}"; do [[ $o -le 255 ]] || return 1; done
  else
    [[ $1 =~ ^[0-9a-fA-F:]+$ && $1 == *:*:* && $1 != *:::* ]]
  fi
}
# probe <addr> <port>: 0 = a connection was made, 1 = refused or timed out; anything else aborts
probe() {
  local rc err
  # shellcheck disable=SC2016 # $1 and $2 are for the inner bash
  err=$(LC_ALL=C timeout "$PROBE_TIMEOUT" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$1" "$2" 2>&1) && return 0
  rc=$?
  [[ $rc -eq 124 ]] && return 1
  [[ $rc -eq 1 && $err =~ (Connection\ refused|timed\ out|unreachable|No\ route) ]] && return 1
  die "probe of $1:$2 could not run (exit $rc): $err"
}
addrs=$(ip -o addr show scope global) || die "ip addr failed, cannot find the box's addresses"
probed=0
while read -r _ dev fam cidr _; do
  [[ -n ${dev:-} ]] || continue
  [[ $dev == tailscale0 ]] && continue
  a=${cidr%/*}
  case $fam in
    inet) valid_ip "$a" && [[ $a != *:* ]] || die "bad IPv4 line from ip: $dev $fam $cidr" ;;
    inet6) valid_ip "$a" && [[ $a == *:* ]] || die "bad IPv6 line from ip: $dev $fam $cidr" ;;
    *) die "unknown address family in line from ip: $dev $fam $cidr" ;;
  esac
  [[ $cidr == */* ]] || die "address without prefix length from ip: $dev $fam $cidr"
  probed=$((probed + 1))
  for p in $ports; do
    if probe "$a" "$p"; then bad "$a:$p answers"; else ok "$a:$p refuses"; fi
  done
done <<<"$addrs"
[[ $probed -gt 0 ]] || bad "no global address found to probe"

# 3. serve config: only :TS_PORT, only the one proxy to AGENT_MONITOR, no funnel, no Services, no Foreground sessions
# (ipn.ServeConfig: TCP, Web, Services, AllowFunnel, Foreground; foreground sessions carry their own TCP/Web/funnel)
serve_json=$(tailscale serve status --json) || die "tailscale serve status failed"
if jq -e --arg port "$TS_PORT" --arg target "$AGENT_MONITOR" '
     ((.TCP // {}) | keys) == [$port]
     and ((.Web // {}) | length) >= 1
     and ((.Web // {}) | keys | all(endswith(":" + $port)))
     and ([(.Web // {})[].Handlers // {} | to_entries[]] | length >= 1 and all(.key == "/" and .value.Proxy == $target))
     and (((.AllowFunnel // {}) | [.[]] | any) | not)
     and ((.Services // {}) | length == 0)
     and ((.Foreground // {}) | length == 0)' <<<"$serve_json" >/dev/null; then
  ok "tailscale serve holds only :$TS_PORT -> $AGENT_MONITOR, no funnel"
else
  bad "tailscale serve config is not exactly :$TS_PORT -> $AGENT_MONITOR: $(jq -c . <<<"$serve_json" 2>&1)"
fi

exit "$fail"
