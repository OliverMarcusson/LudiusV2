# Offline check of set-clock.ps1's all-or-nothing rules: Hyper-V cmdlets, PowerShell Direct and ssh are stubbed (no VM
# is touched). Each case runs a copy of the script in a temp tree with its own .env and build\srv marker.
#   pwsh -NoProfile -File infra/hyperv/test-set-clock.ps1
$ErrorActionPreference = 'Stop'
$dir = Join-Path ([IO.Path]::GetTempPath()) "ludius-set-clock-$PID"
$hv = Join-Path $dir 'infra\hyperv'
New-Item -ItemType Directory -Force (Join-Path $hv 'build\srv') | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'set-clock.ps1') $hv
"LUDIUS_VM=ludius-a`nLUDIUS_ADMIN_USER=.\admin`nLUDIUS_ADMIN_PASSWORD=x`nLUDIUS_SRV_VM=ludius-srv01" | Set-Content (Join-Path $dir '.env')
$marker = Join-Path $hv 'build\srv\clock-jumped'
$state, $log = (Join-Path $dir 'state.json'), (Join-Path $dir 'calls.log')
$runner = Join-Path $dir 'run.ps1'
@'
param($script, $state, $log)
$s = Get-Content $state | ConvertFrom-Json
function Log($m) { Add-Content $log $m }
function Get-VM { [CmdletBinding()] param($Name) if ($Name -eq 'ludius-srv01') { [pscustomobject]@{ Name = $Name; State = $s.srv } } else { [pscustomobject]@{ Name = $Name; State = 'Running' } } }
function Get-VMNetworkAdapter { param($VMName) [pscustomobject]@{ SwitchName = 'Default Switch'; IPAddresses = @('172.20.0.5') } }
function Disable-VMIntegrationService { param([Parameter(Position = 0)]$VMName, $Name) Log "disable $VMName" }
function Enable-VMIntegrationService { param([Parameter(Position = 0)]$VMName, $Name) Log "enable $VMName" }
function Invoke-Command { param($VMName, $Credential, $ScriptBlock, $ArgumentList) Log "guest-set $VMName"; if ($s.guestFail) { throw 'guest unreachable' }; $s.guestTime }
function ssh {
    $epoch = [long](($args[-1] -split '@')[1] -split ' ')[0]
    $t = [DateTimeOffset]::FromUnixTimeSeconds($epoch).LocalDateTime
    Log ("ssh {0}" -f ($(if ([math]::Abs(($t - (Get-Date)).TotalMinutes) -lt 1) { 'now' } else { $t.ToString('s') })))
    if ($s.sshFail) { $global:LASTEXITCODE = 255; return }
    $global:LASTEXITCODE = 0; $t.ToString('s')
}
$a = if ($s.time) { @{ Time = $(if ($s.time -is [datetime]) { $s.time.ToString('s') } else { $s.time }) } } else { @{ Reset = $true } }
try { & $script @a; 'RESULT ok' } catch { "RESULT fail: $_" }
'@ | Set-Content $runner

$T = '2026-10-28T09:00:00'
$cases = @(
    @{ what = 'jump: server then guest, marker left'; srv = 'Running'; guestTime = $T; time = $T
       ok = $true; calls = "disable ludius-srv01|ssh $T|disable ludius-a|guest-set ludius-a"; markerAfter = $true },
    @{ what = 'jump: guest fails, server put back'; srv = 'Running'; guestFail = $true; time = $T
       ok = $false; calls = "disable ludius-srv01|ssh $T|disable ludius-a|guest-set ludius-a|guest-set ludius-a|ssh now|enable ludius-srv01"; markerAfter = $false },
    @{ what = 'jump: guest 1 h off, both put back'; srv = 'Running'; guestTime = '2026-10-28T10:00:00'; time = $T
       ok = $false; calls = "disable ludius-srv01|ssh $T|disable ludius-a|guest-set ludius-a|guest-set ludius-a|enable ludius-a|ssh now|enable ludius-srv01"; markerAfter = $false },
    @{ what = 'jump: server ssh fails, guest untouched'; srv = 'Running'; sshFail = $true; time = $T
       ok = $false; calls = "disable ludius-srv01|ssh $T|ssh now"; markerAfter = $true },
    @{ what = 'reset: saved server holding a jump fails, guest still reset'; srv = 'Saved'; markerBefore = $true; guestTime = 'x'; reset = $true
       ok = $false; calls = 'enable ludius-srv01|guest-set ludius-a|enable ludius-a'; markerAfter = $true },
    @{ what = 'reset: saved server, no jump: ok'; srv = 'Saved'; guestTime = 'x'; reset = $true
       ok = $true; calls = 'enable ludius-srv01|guest-set ludius-a|enable ludius-a'; markerAfter = $false },
    @{ what = 'reset: server ssh fails, guest still reset'; srv = 'Running'; markerBefore = $true; sshFail = $true; guestTime = 'x'; reset = $true
       ok = $false; calls = 'ssh now|enable ludius-srv01|guest-set ludius-a|enable ludius-a'; markerAfter = $true },
    @{ what = 'reset: guest fails, server reset, guest sync still on'; srv = 'Running'; markerBefore = $true; guestFail = $true; reset = $true
       ok = $false; calls = 'ssh now|enable ludius-srv01|enable ludius-srv01|guest-set ludius-a|enable ludius-a'; markerAfter = $false }
)
try {
    foreach ($c in $cases) {
        Remove-Item $log, $marker -ErrorAction SilentlyContinue
        if ($c.markerBefore) { New-Item -ItemType File $marker | Out-Null }
        $c | ConvertTo-Json | Set-Content $state
        $out = & pwsh -NoProfile -NonInteractive -File $runner (Join-Path $hv 'set-clock.ps1') $state $log
        $result = $out | Where-Object { $_ -like 'RESULT*' }
        $calls = (Get-Content $log -ErrorAction SilentlyContinue) -join '|'
        if (($result -eq 'RESULT ok') -ne $c.ok) { throw "$($c.what): $result" }
        if ($calls -ne $c.calls) { throw "$($c.what): calls $calls" }
        if ((Test-Path $marker) -ne $c.markerAfter) { throw "$($c.what): marker $(Test-Path $marker)" }
        "ok: $($c.what)"
    }
}
finally { Remove-Item -Recurse $dir }
