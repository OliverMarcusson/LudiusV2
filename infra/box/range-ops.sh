#!/usr/bin/env bash
# Pre-flight + snapshot (docs/range-operations.md §2-3) and rollback + cleanup (§6), run on the box from anywhere:
#   infra/box/range-ops.sh preflight | rollback
# Every step appends {at, event, step, status, detail} to runs/ops.jsonl; the console's Status page shows the last run.
# runs/ops.jsonl sits outside what pre-flight moves aside, so the log survives its own clearing step.
set -u
cd "$(dirname "$0")/../.."
kind=${1:-}; snap=pre-warp; ts=$(date +%Y%m%d%H%M); log=runs/ops.jsonl; out=$(mktemp)
case $kind in preflight|rollback) ;; *) echo "usage: $0 preflight|rollback" >&2; exit 2 ;; esac

note() { jq -nc --arg e "$kind" --arg s "$1" --arg st "$2" --arg d "${3:-}" '{at: (now | todate), event: $e, step: $s, status: $st, detail: $d}' >> "$log"; }
# step NAME CMD...: run CMD, log running then ok/failed with the last output line, and pass its status on.
step() {
  local name=$1; shift; note "$name" running
  if "$@" > "$out" 2>&1; then note "$name" ok "$(tail -n1 "$out" | cut -c1-300)"; else note "$name" failed "$(tail -n1 "$out" | cut -c1-300)"; return 1; fi
}
abort() { [ -z "${2:-}" ] || note "$1" failed "$2"; note end failed; echo "$kind stopped at $1" >&2; exit 1; }

# The range is pool ludius minus its templates and the other ranges' VMs (vm_prefix in infra/ansible/ranges/*.yml);
# a member with data/personas/<user>.yaml is a persona, the rest servers.
others=$(awk '/^vm_prefix:/ { gsub(/["\r]/, "", $2); print $2 }' infra/ansible/ranges/*.yml 2>/dev/null | jq -Rsc 'split("\n") | map(select(. != ""))')
mapfile -t members < <(pvesh get /pools/ludius --output-format json | jq -r --argjson o "$others" '.members[] | select(.type == "qemu" and .template != 1) | select(.name as $n | all($o[]; . as $p | $n | startswith($p) | not)) | "\(.vmid) \(.name)"')
servers=(); personas=()   # "vmid user" pairs
for m in "${members[@]}"; do u=${m#* ludius-}; [ -f "data/personas/$u.yaml" ] && personas+=("${m%% *} $u") || servers+=("${m%% *}"); done
vms=("${servers[@]}"); for p in "${personas[@]}"; do vms+=("${p%% *}"); done
[ ${#personas[@]} -gt 0 ] || { note begin running; abort "Range VMs" "no persona VMs in pool ludius"; }
note begin running "${#servers[@]} servers, ${#personas[@]} personas"

# has VMID: the snapshot exists by its exact name (pre-warp-social is another one).
has() { qm listsnapshot "$1" | grep -qE "(^|[ >])$snap( |\$)"; }
idle() { ! pgrep -x ludius-rs > /dev/null || { pgrep -ax ludius-rs | head -n1; return 1; }; }
running() { local bad=""; for v in "${vms[@]}"; do [ "$(qm status "$v" | cut -d' ' -f2)" = running ] || bad+=" $v"; done; [ -z "$bad" ] && echo "${#vms[@]} running" || { echo "not running:$bad"; return 1; }; }
nosnap() { local bad=""; for v in "${vms[@]}"; do has "$v" && bad+=" $v"; done; [ -z "$bad" ] && echo "none" || { echo "$snap already on:$bad"; return 1; }; }

clear_runs() {
  local a=/root/warp/pre-runs-$ts; mkdir -p "$a/world" "$a/locks"
  mv runs/range runs/days runs/plans runs/calls runs/monitor-* runs/*-session.jsonl "$a/" 2>/dev/null
  mv runs/world/requests runs/world/state.yaml runs/world/content.jsonl "$a/world/" 2>/dev/null
  mv runs/locks/*.day.json "$a/locks/" 2>/dev/null
  cp runs/openrouter-calls.json "$a/" 2>/dev/null; echo '{"decision": 0, "llm": 0}' > runs/openrouter-calls.json
  echo "moved to $a"
}
# Earlier runs' intranet downloads go to Downloads\earlier-runs-<ts> (moved, never deleted), or the task for them abandons.
downloads() {
  local names; names=$(grep -rh "^files:" data/intranet | sed "s/^files: *//" | tr -d '\r' | tr "," "\n" | sed "s/^ *//;s/ *$//;s/'/''/g" | sort -u | paste -sd"|")
  local r; r=$(qm guest exec "$1" --timeout 60 -- powershell -NoProfile -Command "\$ErrorActionPreference='Stop'; \$d='C:/Users/$2/Downloads'; \$a=\"\$d/earlier-runs-$ts\"; \$n='$names'.Split('|'); @(Get-ChildItem -LiteralPath \$d -File | Where-Object { \$n -contains \$_.Name } | ForEach-Object { New-Item -ItemType Directory -Force \$a > \$null; Move-Item -LiteralPath \$_.FullName -Destination \$a; \$_.Name }).Count") || return 1
  jq -e '.exitcode == 0' <<< "$r" > /dev/null || { jq -r '"exit \(.exitcode): \(."err-data" // "")"' <<< "$r"; return 1; }
  echo "moved $(jq -r '."out-data" // "0"' <<< "$r" | tr -d '\r\n') files"
}
# Only ok on every app counts: canary exits 0 on unknown (agent or LLM down), 1 only on drift.
canary() {
  local apps=(outlook word excel) r; r=$(./ludius-rs --vm "ludius-$1" canary --persona "$1" "${apps[@]}" 2>&1); local rc=$?
  [ $rc = 0 ] && [ "$(grep -c "^  [a-z]*: ok (" <<< "$r")" = ${#apps[@]} ] && { echo "${#apps[@]} apps ok"; return 0; }
  { grep "^  [a-z]*: " <<< "$r" | grep -v ": ok ("; tail -n1 <<< "$r"; } | head -n1; return 1
}
# The runbook's "agent and Temporal up".
services() { local bad=""; for c in ludius-host-agent-1 ludius-temporal-1; do podman ps --format "{{.Names}} {{.Status}}" | grep -q "^$c Up" || bad+=" $c"; done; [ -z "$bad" ] && echo "agent, Temporal up" || { echo "down:$bad"; return 1; }; }
snapshot() {
  for v in "${servers[@]}"; do qm snapshot "$v" "$snap" --vmstate 1 --description "warp, rollback after" > /dev/null 2>&1; done
  for p in "${personas[@]}"; do qm snapshot "${p%% *}" "$snap" --vmstate 1 --description "warp, rollback after" > /dev/null 2>&1 & done; wait
  local bad=""; for v in "${vms[@]}"; do has "$v" || bad+=" $v"; done
  [ -z "$bad" ] && echo "${#vms[@]} VMs" || { echo "missing on:$bad"; return 1; }
}
rollback() { local bad=""; for v in "${vms[@]}"; do qm rollback "$v" "$snap" > /dev/null 2>&1 || bad+=" $v"; done; [ -z "$bad" ] && echo "${#vms[@]} VMs" || { echo "failed on:$bad"; return 1; }; }
# After a RAM rollback the observer's vsock listener is dead although uiprobe runs; restart it and wait for a settled snapshot.
observer() {
  qm guest exec "$1" --timeout 30 -- powershell -NoProfile -Command "Stop-Process -Name uiprobe -Force -EA SilentlyContinue; Start-Sleep 2; Start-ScheduledTask -TaskName RangeAssist" > /dev/null || return 1
  for _ in 1 2 3 4; do sleep 15; timeout 40 ./ludius-rs --vm "ludius-$2" observe --limit 0 2>&1 | grep -m1 -o "settled=[^ ]*" && return 0; done
  echo "no settled snapshot"; return 1
}
delsnap() { local bad=""; for v in "${vms[@]}"; do qm delsnapshot "$v" "$snap" > /dev/null 2>&1 || bad+=" $v"; done; [ -z "$bad" ] && echo "${#vms[@]} VMs" || { echo "failed on:$bad"; return 1; }; }

failed=0
if [ "$kind" = preflight ]; then
  step "No ludius-rs running" idle || abort "No ludius-rs running"
  step "VMs running" running || abort "VMs running"
  step "No $snap snapshot yet" nosnap || abort "No $snap snapshot yet"
  step "Agent and Temporal up" services || abort "Agent and Temporal up"
  # One mail read: outcome checks read mail the same way (warp9 ran with every read unknown after the CA path broke).
  step "Mail read" ./ludius-rs mail check "${personas[0]#* }" || abort "Mail read"
  step "Clear runs" clear_runs
  for p in "${personas[@]}"; do step "Downloads ${p#* }" downloads ${p} || failed=1; done
  for p in "${personas[@]}"; do u=${p#* }; step "Canary $u" canary "$u" || failed=1; done
  [ $failed = 0 ] || abort Snapshot "skipped: fix the failed steps above, then rerun"
  step Snapshot snapshot || abort Snapshot
else
  step "Warp exited" idle || abort "Warp exited"
  mkdir -p /root/warp; step "Archive runs" tar czf "/root/warp/runs-$ts.tgz" --exclude=runs/ops.jsonl runs || failed=1
  step "Roll back" rollback || abort "Roll back"
  step "Clocks to real time" ./ludius-rs range unwarp || failed=1
  for p in "${personas[@]}"; do step "Observer ${p#* }" observer ${p} || failed=1; done
  [ $failed = 0 ] || abort "Drop snapshot" "kept: fix the failed steps above, then qm delsnapshot <vm> $snap"
  step "Drop snapshot" delsnap || failed=1
fi
rm -f "$out"
[ $failed = 0 ] && note end ok || note end failed
exit $failed
