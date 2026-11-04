#!/bin/bash
# Wait for sysprep to shut the golden VM down (success). Sysprep that exits while the VM stays up has failed:
# say so after a 2-minute grace instead of waiting out the 30 minutes, with the tail of its setuperr.log.
set -u
vm=$1
gone=0
probe='if ((Get-ScheduledTaskInfo ludius-sysprep -EA 0).LastRunTime -gt (Get-Date).AddDays(-1) -and -not (Get-Process sysprep -EA 0)) { "SYSPREP-EXITED" }'
for _ in $(seq 360); do
  qm status "$vm" | grep -q stopped && exit 0
  # the guest agent goes away during a real shutdown; only a clear answer counts
  if timeout 30 qm guest exec "$vm" --timeout 20 -- powershell.exe -NoProfile -Command "$probe" 2>/dev/null | grep -q SYSPREP-EXITED; then
    gone=$((gone + 1))
  else
    gone=0
  fi
  if [ "$gone" -ge 12 ]; then
    echo "sysprep exited but VM $vm is still running: sysprep failed. setuperr.log:" >&2
    timeout 30 qm guest exec "$vm" --timeout 20 -- powershell.exe -NoProfile -Command \
      'Get-Content C:\Windows\System32\Sysprep\Panther\setuperr.log -Tail 20' >&2
    exit 1
  fi
  sleep 5
done
echo "sysprep did not shut VM $vm down within 30 min" >&2
exit 1
