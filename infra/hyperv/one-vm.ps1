# Owner's cap: at most one Ludius VM up on this host; anything not Off or Saved counts (Paused, Starting, Saving).
# Every activation (start, restore) runs through Invoke-OneVm: a host-wide named mutex is held across the recheck and
# the activation, so two processes (host agent, new-vm.ps1, new-dc.ps1) can't both see an idle host and both start.
# Fails closed: if the lock can't be taken within -LockSeconds, nothing is activated.
# Dot-source it: . (Join-Path $PSScriptRoot 'one-vm.ps1'). The host agent dot-sources it too (LUDIUS_ONEVM).
function Invoke-OneVm([string]$Name, [scriptblock]$Activate, [int]$LockSeconds = 60) {
    $m = [Threading.Mutex]::new($false, 'Global\ludius-vm-activation')
    try {
        try { $got = $m.WaitOne([TimeSpan]::FromSeconds($LockSeconds)) }
        catch [Threading.AbandonedMutexException] { $got = $true }  # holder died; we own it now
        if (-not $got) { throw "one-VM cap: activation lock busy for $LockSeconds s; $Name not activated" }
        try {
            $up = @(Get-VM -Name 'ludius-*' | Where-Object { $_.State -notin 'Off', 'Saved' -and $_.Name -ne $Name })
            if ($up) { throw "one-VM cap: $($up.Name -join ', ') is up; save it before activating $Name" }
            & $Activate
        } finally { $m.ReleaseMutex() }
    } finally { $m.Dispose() }
}
