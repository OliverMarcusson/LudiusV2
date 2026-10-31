#!/usr/bin/env bash
set -euo pipefail

: "${AGENT_CONSOLE:=http://127.0.0.1:7452}"
: "${TS_PORT:=8452}"
: "${PVE_TS_PORT:=8453}"
: "${PVE_TARGET:=https+insecure://127.0.0.1:8006}"
: "${PUBLIC_TCP_ALLOW:=22}"
: "${PROBE_TIMEOUT:=3}"
agent_port=${AGENT_CONSOLE##*:}
pve_port=${PVE_TARGET##*:}
ports=$(printf '%s\n' 7452 7453 8006 3128 85 "$agent_port" "$pve_port" "$TS_PORT" "$PVE_TS_PORT" | sort -u)
fail=0
bad() { echo "FAIL: $*"; fail=1; }
ok() { echo "ok:   $*"; }
die() { echo "ABORT: $*" >&2; exit 2; }

for t in ss ip tailscale jq timeout bash awk sort grep tr mktemp; do
  command -v "$t" >/dev/null || die "$t is missing (apt-get install jq iproute2 coreutils ...)"
done

ss_out=$(ss -H -ltn) || die "ss failed, cannot list listeners"
[[ -n $ss_out ]] || die "ss listed no listeners at all"
agent_up=; pve_up=
while read -r addr; do
  [[ $addr =~ ^(.+):([0-9]+)$ ]] || die "unparsable listener address: $addr"
  host=${BASH_REMATCH[1]}; port=${BASH_REMATCH[2]}
  host=${host%%\%*}
  case $host in
    127.* | '[::1]' | '[::ffff:127.'*) loc=local ;;
    100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].* | '[fd7a:115c:a1e0:'*) loc=tailnet ;;
    10.77.* | 10.78.*) loc=range ;;
    *) loc=public ;;
  esac
  if grep -qx "$port" <<<"$ports"; then
    [[ $loc == local || $loc == tailnet ]] || { bad "port $port listens on $host (must be loopback)"; continue; }
  elif [[ $loc == public ]] && ! grep -qw -- "$port" <<<"${PUBLIC_TCP_ALLOW//,/ }"; then
    bad "unexpected public listener $host:$port (allowed public TCP ports: $PUBLIC_TCP_ALLOW)"
  fi
  [[ $addr == "${AGENT_CONSOLE#http://}" ]] && agent_up=1
  [[ $addr == "127.0.0.1:$pve_port" ]] && pve_up=1
done < <(awk '{print $4}' <<<"$ss_out")
if [[ -n $agent_up ]]; then ok "agent monitor on ${AGENT_CONSOLE#http://}, no wider listener on $(tr '\n' ' ' <<<"$ports")"
else echo "warn: nothing on ${AGENT_CONSOLE#http://} yet (agent not running?)"; fi
[[ -n $pve_up ]] || echo "warn: nothing on 127.0.0.1:$pve_port yet (pveproxy not running, or not bound to loopback?)"

valid_ip() {
  local o
  if [[ $1 =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for o in "${BASH_REMATCH[@]:1}"; do [[ $o -le 255 ]] || return 1; done
  else
    [[ $1 =~ ^[0-9a-fA-F:]+$ && $1 == *:*:* && $1 != *:::* ]]
  fi
}
probe() {
  local rc err
  # shellcheck disable=SC2016
  err=$(LC_ALL=C timeout "$PROBE_TIMEOUT" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$1" "$2" 2>&1) && return 0
  rc=$?
  [[ $rc -eq 124 ]] && return 1
  [[ $rc -eq 1 && $err =~ (Connection\ refused|timed\ out|unreachable|No\ route) ]] && return 1
  echo "exit $rc: $err"
  return 2
}
addrs=$(ip -o addr show scope global) || die "ip addr failed, cannot find the box's addresses"
probe_addrs=()
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
  probe_addrs+=("$a")
done <<<"$addrs"
if [[ ${#probe_addrs[@]} -eq 0 ]]; then bad "no global address found to probe"; else
  pdir=$(mktemp -d)
  trap 'rm -rf "$pdir"' EXIT
  n=0
  for a in "${probe_addrs[@]}"; do
    for p in $ports; do
      n=$((n + 1))
      echo "$a:$p" >"$pdir/$n.target"
      ( rc=0; probe "$a" "$p" >"$pdir/$n.msg" || rc=$?; echo "$rc" >"$pdir/$n.rc" ) &
    done
  done
  wait
  for ((i = 1; i <= n; i++)); do
    target=$(<"$pdir/$i.target")
    [[ -s $pdir/$i.rc ]] || die "probe of $target left no result"
    case $(<"$pdir/$i.rc") in
      0) bad "$target answers" ;;
      1) ok "$target refuses" ;;
      *) die "probe of $target could not run ($(<"$pdir/$i.msg"))" ;;
    esac
  done
fi

serve_json=$(tailscale serve status --json) || die "tailscale serve status failed"
if jq -e --arg p1 "$TS_PORT" --arg t1 "$AGENT_CONSOLE" --arg p2 "$PVE_TS_PORT" --arg t2 "$PVE_TARGET" '
     def want: {($p1): $t1, ($p2): $t2};
     [(.Web // {}) | to_entries[] | (.key | split(":") | last) as $port
        | (.value.Handlers // {}) | to_entries[] | {port: $port, path: .key, proxy: .value.Proxy}] as $h
     | ((.TCP // {}) | keys | sort) == ([$p1, $p2] | sort)
       and ($h | map(.port) | unique) == ([$p1, $p2] | sort)
       and ($h | all(.path == "/" and .proxy == want[.port]))
       and (((.AllowFunnel // {}) | [.[]] | any) | not)
       and ((.Services // {}) | length == 0)
       and ((.Foreground // {}) | length == 0)' <<<"$serve_json" >/dev/null; then
  ok "tailscale serve holds only :$TS_PORT -> $AGENT_CONSOLE and :$PVE_TS_PORT -> $PVE_TARGET, no funnel"
else
  bad "tailscale serve config is not exactly :$TS_PORT -> $AGENT_CONSOLE and :$PVE_TS_PORT -> $PVE_TARGET: $(jq -c . <<<"$serve_json" 2>&1)"
fi

exit "$fail"
