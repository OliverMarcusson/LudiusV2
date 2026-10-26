# Checks clock-sweep.ps1 against a stub set-clock.ps1: no VM is touched.
#  fresh lease          -> left alone, no reset
#  stale lease          -> reset for its VM, then deleted
#  stale, reset fails   -> kept and reported, so the next sweep retries
$ErrorActionPreference = 'Stop'
$dir = Join-Path ([IO.Path]::GetTempPath()) "ludius-clocksweep-$PID"
$locks = Join-Path $dir 'locks'
New-Item -ItemType Directory -Force $locks | Out-Null
$calls = Join-Path $dir 'calls.txt'
$stub = Join-Path $dir 'set-clock.ps1'
Set-Content $stub @"
param([string]`$Time, [switch]`$Reset, [string]`$VM)
Add-Content '$calls' "`$VM reset=`$Reset"
if (`$VM -eq 'ludius-down') { exit 1 }
"@
function Lease($vm, $ageSeconds) {
    $renewed = [datetime]::UtcNow.AddSeconds(-$ageSeconds).ToString('yyyy-MM-ddTHH:mm:ss+00:00')
    Set-Content (Join-Path $locks "clock-$vm.json") (@{ vm = $vm; pid = 4242; renewed = $renewed } | ConvertTo-Json)
}
Lease 'ludius-live' 20
Lease 'ludius-dead' 600
Lease 'ludius-down' 600

$out = & pwsh -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'clock-sweep.ps1') -Locks $locks -SetClock $stub
$out
$reset = @(Get-Content $calls)
$left = @(Get-ChildItem $locks -Filter 'clock-*.json' | ForEach-Object Name | Sort-Object)
"resets: $($reset -join '; ')"
"leases left: $($left -join ', ')"
if ($reset.Count -ne 2 -or $reset -notcontains 'ludius-dead reset=True' -or $reset -notcontains 'ludius-down reset=True') { throw 'wrong resets' }
if (($left -join ',') -ne 'clock-ludius-down.json,clock-ludius-live.json') { throw 'wrong leases left' }
if (-not ($out -like '*ludius-down*reset FAILED*')) { throw 'failed reset not reported' }
'ok: fresh lease kept, orphan reset and removed, failed reset kept for retry'
Remove-Item -Recurse $dir
