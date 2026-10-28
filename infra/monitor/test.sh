#!/usr/bin/env bash
# Offline tests for check.sh and bring-up.sh's identity gate, against stubbed ss, ip and tailscale.
# Needs bash, jq, python3 (a throwaway listener for the probe cases).   infra/monitor/test.sh
# shellcheck disable=SC2016,SC2329,SC2001,SC2015 # stub bodies are literal; helpers run through expect; A && B || fail is intended
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tmp=$(mktemp -d)
trap 'kill ${lpid:-} 2>/dev/null; rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
for t in ss ip tailscale; do
  printf '#!/usr/bin/env bash\nsource "$STUB_DIR/$(basename "$0").sh"\n' >"$tmp/bin/$t"
  chmod +x "$tmp/bin/$t"
done
export STUB_DIR=$tmp PATH="$tmp/bin:$PATH"

cat >"$tmp/ss.sh" <<'EOF'
[[ ${SS_RC:-0} -eq 0 ]] || { echo "ss: boom" >&2; exit "$SS_RC"; }
printf '%s\n' "${SS_OUT-LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 128 [::]:22 [::]:*
LISTEN 0 512 127.0.0.1:7452 0.0.0.0:*
LISTEN 0 4096 127.0.0.1:8006 0.0.0.0:*
LISTEN 0 128 127.0.0.1:85 0.0.0.0:*
LISTEN 0 100 127.0.0.1:25 0.0.0.0:*
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*}"
EOF
cat >"$tmp/ip.sh" <<'EOF'
[[ ${IP_RC:-0} -eq 0 ]] || exit "$IP_RC"
printf '%s\n' "${IP_OUT-2: eth0    inet 192.0.2.10/24 brd 192.0.2.255 scope global eth0
2: eth0    inet6 2001:db8::10/64 scope global
3: tailscale0    inet 100.100.1.1/32 scope global tailscale0}"
EOF
# serve status as tailscaled prints it: <part> builds the monitor (8452) and Proxmox (8453) halves, serve <extra> joins them
m_tcp='"8452":{"HTTP":true}'
m_web='"h.ts.net:8452":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:7452"}}}'
p_tcp='"8453":{"HTTP":true}'
p_web='"h.ts.net:8453":{"Handlers":{"/":{"Proxy":"https+insecure://127.0.0.1:8006"}}}'
serve() { echo "{\"TCP\":{$m_tcp,$p_tcp},\"Web\":{$m_web,$p_web}${1:-}}"; }
good_serve=$(serve "")
good_status='{"Self":{"Tags":["tag:ludius-box"],"UserID":123,"HostName":"ludius-box"},"User":{"123":{"LoginName":"tagged-devices"}}}'
good_prefs='{"AdvertiseRoutes":null,"ExitNodeID":"","ExitNodeIP":""}'
cat >"$tmp/tailscale.sh" <<'EOF'
case "$1 $2" in
  "serve status") [[ ${SERVE_RC:-0} -eq 0 ]] || exit "$SERVE_RC"; printf '%s\n' "$SERVE_JSON" ;;
  "status --json") printf '%s\n' "$TS_STATUS" ;;
  "debug prefs") printf '%s\n' "$TS_PREFS" ;;
  "ip -4") echo 100.100.1.1 ;;
  "serve --http="*) echo "tailscale $*" >>"$STUB_DIR/ts.log"; exit "${SERVE_OFF_RC:-0}" ;;
  "logout "*) echo "tailscale logout" >>"$STUB_DIR/ts.log" ;;
  *) echo "unexpected tailscale $*" >&2; exit 9 ;;
esac
EOF
export SERVE_JSON=$good_serve TS_STATUS=$good_status TS_PREFS=$good_prefs PROBE_TIMEOUT=0.3

fails=0
# expect <name> <exit code wanted, or "nonzero"> <command...>
expect() {
  local name=$1 want=$2 rc; shift 2
  "$@" >"$tmp/out" 2>&1; rc=$?
  if [[ $want == nonzero && $rc -ne 0 ]] || [[ $rc == "$want" ]]; then echo "pass: $name (exit $rc)"
  else echo "FAIL: $name: exit $rc, wanted $want"; sed 's/^/      /' "$tmp/out"; fails=$((fails + 1)); fi
}
check() { "$here/check.sh"; }
# the same call with one variable changed
with() { env "$@" "$here/check.sh"; }

expect "check: clean box passes" 0 check
expect "check: ss fails -> abort, not 'no listeners'" 2 with SS_RC=1
expect "check: ss prints nothing -> abort" 2 with SS_OUT=
expect "check: ss line unparsable -> abort" 2 with "SS_OUT=garbage"
expect "check: ip fails -> abort" 2 with IP_RC=1
expect "check: serve status fails -> abort" 2 with SERVE_RC=1
expect "check: IPv4 wildcard on 7452" 1 with "SS_OUT=LISTEN 0 512 0.0.0.0:7452 0.0.0.0:*"
expect "check: IPv6 wildcard on 7452" 1 with "SS_OUT=LISTEN 0 512 [::]:7452 [::]:*"
expect "check: IPv6 wildcard on 8452" 1 with "SS_OUT=LISTEN 0 512 [::]:8452 [::]:*"
expect "check: public IPv6 address on 7452" 1 with "SS_OUT=LISTEN 0 512 [2001:db8::10]:7452 [::]:*"
expect "check: IPv6 loopback only is fine" 0 with "SS_OUT=LISTEN 0 512 [::1]:7452 [::]:*"
expect "check: fallback proxy on 0.0.0.0:7453" 1 with "SS_OUT=LISTEN 0 512 0.0.0.0:7453 0.0.0.0:*"
expect "check: serve monitor to another target" 1 with "SERVE_JSON=$(echo "$good_serve" | sed 's/127.0.0.1:7452/127.0.0.1:22/')"
expect "check: serve has an extra https port" 1 with 'SERVE_JSON={"TCP":{"8452":{"HTTP":true},"8453":{"HTTP":true},"443":{"HTTPS":true}},"Web":{"h:8452":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:7452"}}},"h:8453":{"Handlers":{"/":{"Proxy":"https+insecure://127.0.0.1:8006"}}}}}'
expect "check: funnel on" 1 with "SERVE_JSON=$(serve ',"AllowFunnel":{"h.ts.net:8452":true}')"
expect "check: funnel on the Proxmox port" 1 with "SERVE_JSON=$(serve ',"AllowFunnel":{"h.ts.net:8453":true}')"
expect "check: funnel entry false is fine" 0 with "SERVE_JSON=$(serve ',"AllowFunnel":{"h.ts.net:8453":false}')"
expect "check: serve empty" 1 with 'SERVE_JSON={}'
expect "check: foreground session with its own port" 1 with "SERVE_JSON=$(serve ',"Foreground":{"s1":{"TCP":{"9000":{"HTTP":true}}}}')"
expect "check: foreground session with funnel" 1 with "SERVE_JSON=$(serve ',"Foreground":{"s1":{"AllowFunnel":{"h:443":true}}}')"
expect "check: empty Foreground is fine" 0 with "SERVE_JSON=$(serve ',"Foreground":{}')"

# the Proxmox mapping: exactly :8453 -> https+insecure://127.0.0.1:8006
expect "check: Proxmox mapping missing" 1 with "SERVE_JSON={\"TCP\":{$m_tcp},\"Web\":{$m_web}}"
expect "check: only the Proxmox mapping, monitor missing" 1 with "SERVE_JSON={\"TCP\":{$p_tcp},\"Web\":{$p_web}}"
expect "check: Proxmox mapping without +insecure" 1 with "SERVE_JSON=$(echo "$good_serve" | sed 's|https+insecure://127.0.0.1:8006|https://127.0.0.1:8006|')"
expect "check: Proxmox mapping over plain http" 1 with "SERVE_JSON=$(echo "$good_serve" | sed 's|https+insecure://127.0.0.1:8006|http://127.0.0.1:8006|')"
expect "check: Proxmox port to another target" 1 with "SERVE_JSON=$(echo "$good_serve" | sed 's|https+insecure://127.0.0.1:8006|https+insecure://127.0.0.1:22|')"
expect "check: ports swapped between targets" 1 with "SERVE_JSON=$(echo "$good_serve" | sed -e 's|127.0.0.1:7452|@|' -e 's|https+insecure://127.0.0.1:8006|http://127.0.0.1:7452|' -e 's|@|https+insecure://127.0.0.1:8006|')"
expect "check: Proxmox mapping has a second path" 1 with 'SERVE_JSON={"TCP":{"8452":{"HTTP":true},"8453":{"HTTP":true}},"Web":{"h:8452":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:7452"}}},"h:8453":{"Handlers":{"/":{"Proxy":"https+insecure://127.0.0.1:8006"},"/x":{"Proxy":"http://127.0.0.1:22"}}}}}'
expect "check: a third port mapped" 1 with 'SERVE_JSON={"TCP":{"8452":{"HTTP":true},"8453":{"HTTP":true},"8454":{"HTTP":true}},"Web":{"h:8452":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:7452"}}},"h:8453":{"Handlers":{"/":{"Proxy":"https+insecure://127.0.0.1:8006"}}},"h:8454":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:22"}}}}}'
expect "check: a Service is configured" 1 with "SERVE_JSON=$(serve ',"Services":{"svc:x":{}}')"

# Proxmox ports are loopback-only: pveproxy 8006, spiceproxy 3128, pvedaemon 85
expect "check: pveproxy on 0.0.0.0:8006" 1 with "SS_OUT=LISTEN 0 4096 0.0.0.0:8006 0.0.0.0:*"
expect "check: pveproxy on [::]:8006" 1 with "SS_OUT=LISTEN 0 4096 [::]:8006 [::]:*"
expect "check: pveproxy on a public address" 1 with "SS_OUT=LISTEN 0 4096 192.0.2.10:8006 0.0.0.0:*"
expect "check: spiceproxy on *:3128" 1 with "SS_OUT=LISTEN 0 4096 *:3128 *:*"
expect "check: pvedaemon on 0.0.0.0:85" 1 with "SS_OUT=LISTEN 0 4096 0.0.0.0:85 0.0.0.0:*"
expect "check: pveproxy on loopback and ::1 is fine" 0 with "SS_OUT=$(printf 'LISTEN 0 4096 127.0.0.1:8006 0.0.0.0:*
LISTEN 0 4096 [::1]:8006 [::]:*
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*')"
expect "check: custom Proxmox ports are probed (8453 wildcard)" 1 with "SS_OUT=LISTEN 0 4096 0.0.0.0:8453 0.0.0.0:*"

# any other public TCP listener (rpcbind, postfix, ...) fails; loopback, tailnet and the range bridges do not
expect "check: rpcbind on 0.0.0.0:111" 1 with "SS_OUT=$(printf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:111 0.0.0.0:*')"
expect "check: rpcbind on [::]:111" 1 with "SS_OUT=$(printf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 4096 [::]:111 [::]:*')"
expect "check: postfix on 0.0.0.0:25" 1 with "SS_OUT=$(printf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 100 0.0.0.0:25 0.0.0.0:*')"
expect "check: public listener allowed by PUBLIC_TCP_ALLOW" 0 with PUBLIC_TCP_ALLOW=22,111 "SS_OUT=$(printf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:111 0.0.0.0:*')"
expect "check: loopback, tailnet and bridge listeners are not public" 0 with "SS_OUT=$(printf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 4096 127.0.0.1:111 0.0.0.0:*
LISTEN 0 4096 100.100.1.1:9999 0.0.0.0:*
LISTEN 0 4096 10.77.0.1:7451 0.0.0.0:*
LISTEN 0 4096 [::1]:25 [::]:*')"

# a probe that cannot run is an abort, never a pass; malformed ip output is an abort, never a probed address
mkdir "$tmp/noprobe" "$tmp/oddprobe"
printf '#!/usr/bin/env bash
exit 126
' >"$tmp/noprobe/timeout"
printf '#!/usr/bin/env bash
echo "bash: something odd" >&2
exit 1
' >"$tmp/oddprobe/timeout"
chmod +x "$tmp/noprobe/timeout" "$tmp/oddprobe/timeout"
expect "check: probe tool cannot run (exit 126) -> abort" 2 with "PATH=$tmp/noprobe:$PATH"
expect "check: probe fails with an unknown error -> abort" 2 with "PATH=$tmp/oddprobe:$PATH"
expect "check: ip octet out of range -> abort" 2 with "IP_OUT=2: eth0    inet 999.1.1.1/24 scope global eth0"
expect "check: ip address without prefix -> abort" 2 with "IP_OUT=2: eth0    inet 192.0.2.10 scope global eth0"
expect "check: ip garbage IPv6 -> abort" 2 with "IP_OUT=2: eth0    inet6 not-an-address/64 scope global"
expect "check: ip IPv6 literal under inet -> abort" 2 with "IP_OUT=2: eth0    inet 2001:db8::10/64 scope global"
expect "check: ip unknown family -> abort" 2 with "IP_OUT=2: eth0    link/ether 192.0.2.10/24 scope global"
expect "check: no global address at all fails" 1 with "IP_OUT=3: tailscale0    inet 100.100.1.1/32 scope global tailscale0"

# a connect that succeeds on a global address fails the check, in both families
for py in python3 python; do "$py" -c "" 2>/dev/null && break; done # the Windows python3 is a Store shim
for fam in "127.0.0.1 inet" "::1 inet6"; do
  read -r addr inet <<<"$fam"
  "$py" -c 'import socket,sys
f = socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET
s = socket.socket(f); s.bind((sys.argv[1], 18452)); s.listen(5)
while True: s.accept()' "$addr" &
  lpid=$!
  sleep 1
  expect "check: $inet probe that connects fails" 1 with TS_PORT=18452 "IP_OUT=2: lo    $inet $addr/64 scope global" "SERVE_JSON=$(serve | sed s/8452/18452/g)"
  grep -q "$addr:18452 answers" "$tmp/out" || { echo "FAIL: $inet: the 18452 listener was not what failed the check"; fails=$((fails + 1)); }
  kill "$lpid"; wait "$lpid" 2>/dev/null; lpid=
done

# bring-up: an enrolled box must have exactly the box identity before serve is published
bring() { DRY_RUN=1 "$here/bring-up.sh"; }
expect "bring-up: tagged box proceeds to serve" 0 bring
grep -q '^+ tailscale serve --bg --http=8452 http://127.0.0.1:7452$' "$tmp/out" || { echo "FAIL: monitor serve not printed for a good identity"; fails=$((fails + 1)); }
grep -q '^+ tailscale serve --bg --http=8453 https+insecure://127.0.0.1:8006$' "$tmp/out" || { echo "FAIL: Proxmox serve not printed for a good identity"; fails=$((fails + 1)); }
[[ $(grep -n -m1 '^+ bind_pve_loopback' "$tmp/out" | cut -d: -f1) -lt $(grep -n -m1 '^+ tailscale serve' "$tmp/out" | cut -d: -f1) ]]   || { echo "FAIL: pveproxy must be bound to loopback before serve is published"; fails=$((fails + 1)); }
bad_id() { env "$@" DRY_RUN=1 "$here/bring-up.sh"; }
no_serve() { grep -q 'tailscale serve' "$tmp/out" && { echo "FAIL: serve printed after abort"; fails=$((fails + 1)); }; }
# Tailscale 1.104.4 on the box: a tagged device's own login is its MagicDNS name, not "tagged-devices"
expect "bring-up: tagged box, 1.104 login shape" 0 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":77,"HostName":"ludius-box"},"User":{"77":{"LoginName":"ludius-box.tailec5e26.ts.net"},"5":{"LoginName":"owner@example.com"}}}'
expect "bring-up: login that only ends in ts.net" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":5},"User":{"5":{"LoginName":"someone@x.tailnet.ts.net"}}}'
no_serve
expect "bring-up: user-owned (untagged)" 1 bad_id 'TS_STATUS={"Self":{"Tags":null,"UserID":5},"User":{"5":{"LoginName":"someone@example.com"}}}'
no_serve
expect "bring-up: extra tag" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box","tag:homelab"],"UserID":123},"User":{"123":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: other tag" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:homelab"],"UserID":123},"User":{"123":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: tagged but a user owner listed" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":5},"User":{"5":{"LoginName":"someone@example.com"}}}'
no_serve
expect "bring-up: advertises a route" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":["10.0.0.0/24"]}'
no_serve
expect "bring-up: advertises an exit node" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":["0.0.0.0/0","::/0"]}'
no_serve
expect "bring-up: uses an exit node" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":null,"ExitNodeID":"nabc","ExitNodeIP":""}'
no_serve
# a field that is missing or the wrong type is "cannot tell", not "safe"
expect "bring-up: status has no User map" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":123}}'
no_serve
expect "bring-up: status has no UserID" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"]},"User":{"123":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: User entry for UserID is missing" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":123},"User":{"999":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: LoginName missing" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":123},"User":{"123":{}}}'
no_serve
expect "bring-up: Tags is a string" 1 bad_id 'TS_STATUS={"Self":{"Tags":"tag:ludius-box","UserID":123},"User":{"123":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: UserID is a string" 1 bad_id 'TS_STATUS={"Self":{"Tags":["tag:ludius-box"],"UserID":"123"},"User":{"123":{"LoginName":"tagged-devices"}}}'
no_serve
expect "bring-up: prefs is {}" 1 bad_id 'TS_PREFS={}'
no_serve
expect "bring-up: prefs lack AdvertiseRoutes" 1 bad_id 'TS_PREFS={"ExitNodeID":"","ExitNodeIP":""}'
no_serve
expect "bring-up: prefs lack ExitNodeIP" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":null,"ExitNodeID":""}'
no_serve
expect "bring-up: prefs ExitNodeID is null" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":null,"ExitNodeID":null,"ExitNodeIP":""}'
no_serve
expect "bring-up: AdvertiseRoutes is a string" 1 bad_id 'TS_PREFS={"AdvertiseRoutes":"","ExitNodeID":"","ExitNodeIP":""}'
no_serve
expect "bring-up: empty AdvertiseRoutes array is fine" 0 bad_id 'TS_PREFS={"AdvertiseRoutes":[],"ExitNodeID":"","ExitNodeIP":""}'

# homelab removal: only an ordered marker pair is touched; a failed validate puts the file back
cat >"$tmp/caddy.sh" <<'STUB'
echo "caddy $*" >>"$STUB_DIR/caddy.log"
[[ $1 != validate || ${CADDY_VALIDATE_RC:-0} -eq 0 ]]
STUB
site=$'a.example {\n}\n'
block=$'# BEGIN ludius-monitor\nmonitor.ludius.se {\n}\n# END ludius-monitor\n'
remove() { env CADDY="bash $tmp/caddy.sh" "$@" "$here/homelab/remove-monitor.sh" "$tmp/Caddyfile"; }
# fixture <content>: a fresh Caddyfile and no leftovers
fixture() { rm -f "$tmp"/Caddyfile* "$tmp/caddy.log"; printf '%s' "$1" >"$tmp/Caddyfile"; cp "$tmp/Caddyfile" "$tmp/before"; }
untouched() { if cmp -s "$tmp/before" "$tmp/Caddyfile" && [[ ! -e $tmp/caddy.log ]]; then :; else echo "FAIL: $1: file changed or caddy ran"; fails=$((fails + 1)); fi; }

fixture "$site$block$site"
expect "remove: one ordered pair" 0 remove
cmp -s "$tmp/Caddyfile" <(printf '%s%s' "$site" "$site") || { echo "FAIL: wrong result"; fails=$((fails + 1)); }
[[ -e $tmp/Caddyfile.bak-before-ludius-monitor-teardown ]] || { echo "FAIL: no backup"; fails=$((fails + 1)); }
[[ $(cut -d' ' -f1-2 "$tmp/caddy.log" | tr '\n' ,) == "caddy validate,caddy reload," ]] || { echo "FAIL: validate then reload expected"; fails=$((fails + 1)); }

fixture "$site$(sed -e 's/__BOX_TAILNET_IP__/100.1.2.3/' -e 's/__BOX_DNS_NAME__/box.t.ts.net/' -e 's/__TS_PORT__/8452/g' "$here/homelab/monitor.caddy")"$'\n'
expect "remove: the shipped monitor.caddy, as appended by the README" 0 remove
grep -q ludius "$tmp/Caddyfile" && { echo "FAIL: shipped block still present"; fails=$((fails + 1)); }

fixture "$site"$'# BEGIN ludius-monitor\nmonitor.ludius.se {\n}\n'"$site"
expect "remove: END missing -> abort, file untouched" 1 remove; untouched "END missing"
fixture "$site$block$block"
expect "remove: two pairs -> abort" 1 remove; untouched "two pairs"
fixture "$site"$'# END ludius-monitor\nx\n# BEGIN ludius-monitor\n'"$site"
expect "remove: END before BEGIN -> abort" 1 remove; untouched "reversed"
fixture "$site"
expect "remove: no markers -> abort" 1 remove; untouched "no markers"
fixture "$site$block"
expect "remove: validate fails -> restored, no reload" 1 remove CADDY_VALIDATE_RC=1
if cmp -s "$tmp/before" "$tmp/Caddyfile" && ! grep -q '^caddy reload' "$tmp/caddy.log"; then :; else echo "FAIL: not restored, or reloaded"; fails=$((fails + 1)); fi

fixture "$site$block$(sed -e 's/__BOX_TAILNET_IP__/100.1.2.3/' -e 's/__BOX_DNS_NAME__/box.t.ts.net/' -e 's/__PVE_TS_PORT__/8453/g' "$here/homelab/pve.caddy")"$'\n'"$site"
expect "remove: BLOCK=ludius-pve takes only the Proxmox block" 0 remove BLOCK=ludius-pve
grep -q 'pve.ludius.se' "$tmp/Caddyfile" && { echo "FAIL: pve block still present"; fails=$((fails + 1)); }
grep -q '^# BEGIN ludius-monitor' "$tmp/Caddyfile" || { echo "FAIL: monitor block was removed too"; fails=$((fails + 1)); }
[[ -e $tmp/Caddyfile.bak-before-ludius-pve-teardown ]] || { echo "FAIL: no ludius-pve backup"; fails=$((fails + 1)); }
expect "remove: default BLOCK then takes the monitor block" 0 remove
grep -q ludius "$tmp/Caddyfile" && { echo "FAIL: blocks still present"; fails=$((fails + 1)); }
fixture "$site$block"
expect "remove: BLOCK=ludius-pve without a Proxmox block -> abort" 1 remove BLOCK=ludius-pve; untouched "no pve markers"
expect "remove: bad BLOCK name -> abort" 1 remove 'BLOCK=a b'; untouched "bad BLOCK"
grep -q 'claustra_forward http://127.0.0.1:18453' "$here/homelab/pve.caddy" && grep -q 'header_up Host __BOX_DNS_NAME__:__PVE_TS_PORT__' "$here/homelab/pve.caddy" || { echo "FAIL: pve.caddy lost its forward or its Host hop"; fails=$((fails + 1)); }

# bring-up: pveproxy is bound to loopback (only restarted when it changed), SPICE proxy and rpcbind masked
cat >"$tmp/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >>"$STUB_DIR/sysctl.log"
[[ $1 != cat ]] || [[ $2 != rpcbind.socket ]] # no rpcbind.socket on this box
STUB
chmod +x "$tmp/bin/systemctl"
eval "$(sed -n '/^bind_pve_loopback() {/,/^}/p' "$here/bring-up.sh")"
export PVEPROXY_DEFAULT=$tmp/pveproxy
rm -f "$tmp/sysctl.log"
expect "bring-up: bind_pve_loopback on a fresh box" 0 bind_pve_loopback
grep -qx 'LISTEN_IP="127.0.0.1"' "$tmp/pveproxy" || { echo "FAIL: LISTEN_IP not written"; fails=$((fails + 1)); }
grep -q '^systemctl restart pveproxy' "$tmp/sysctl.log" || { echo "FAIL: pveproxy not restarted"; fails=$((fails + 1)); }
grep -q '^systemctl mask --now spiceproxy.service' "$tmp/sysctl.log" && grep -q '^systemctl mask --now rpcbind.service' "$tmp/sysctl.log" \
  || { echo "FAIL: spiceproxy/rpcbind not masked"; fails=$((fails + 1)); }
grep -q 'mask --now rpcbind.socket' "$tmp/sysctl.log" && { echo "FAIL: masked a unit that does not exist"; fails=$((fails + 1)); }
rm -f "$tmp/sysctl.log"
expect "bring-up: bind_pve_loopback again" 0 bind_pve_loopback
grep -q 'restart pveproxy' "$tmp/sysctl.log" && { echo "FAIL: pveproxy restarted though nothing changed"; fails=$((fails + 1)); }
printf 'LISTEN_IP="0.0.0.0"\n' >"$tmp/pveproxy"; rm -f "$tmp/sysctl.log"
expect "bring-up: bind_pve_loopback replaces a wider LISTEN_IP" 0 bind_pve_loopback
[[ $(cat "$tmp/pveproxy") == *'LISTEN_IP="127.0.0.1"'* && $(cat "$tmp/pveproxy") != *0.0.0.0* ]] || { echo "FAIL: wider LISTEN_IP left in place"; fails=$((fails + 1)); }

# teardown: both mappings go, and a missing one does not stop the logout
tear() { env "$@" "$here/teardown.sh"; }
expect "teardown: dry run lists both mappings and the logout" 0 tear DRY_RUN=1
grep -q 'http=8452 off' "$tmp/out" && grep -q 'http=8453 off' "$tmp/out" && grep -q 'tailscale logout' "$tmp/out" || { echo "FAIL: teardown dry run incomplete"; fails=$((fails + 1)); }
rm -f "$tmp/ts.log"
expect "teardown: serve off fails (nothing mapped) -> still logs out" 0 tear SERVE_OFF_RC=1
grep -q logout "$tmp/ts.log" && grep -q 'http=8453 off' "$tmp/ts.log" || { echo "FAIL: logout or 8453 off skipped after a failing serve off"; fails=$((fails + 1)); }

echo "$fails failed"
exit $((fails > 0))
