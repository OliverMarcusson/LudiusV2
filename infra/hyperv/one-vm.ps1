# Owner's cap: at most one Ludius VM up on this host; anything not Off or Saved counts (Paused, Starting, Saving).
# Every activation (start, restore) runs through Invoke-OneVm: a host-wide named mutex is held across the recheck and
# the activation, so two processes (host agent, new-vm.ps1, new-dc.ps1) can't both see an idle host and both start.
# Fails closed: if the lock can't be taken within -LockSeconds, nothing is activated.
# Exempt: the range's Linux infrastructure server (Samba AD DC + grommunio, owner-approved 27 Oct 2026) runs beside
# the one persona VM, in either start order. It is identified by the Hyper-V VM Id new-srv.ps1 recorded when it
# created it (build\srv\vm-id), never by name: another VM given the same name has another Id and is capped.
# Dot-source it: . (Join-Path $PSScriptRoot 'one-vm.ps1'). The host agent dot-sources it too (LUDIUS_ONEVM).
$OneVmExemptIdFile = Join-Path $PSScriptRoot 'build\srv\vm-id'
function Get-OneVmExemptIds {
    if (-not (Test-Path -LiteralPath $OneVmExemptIdFile)) { return @() }
    @(Get-Content -LiteralPath $OneVmExemptIdFile | Where-Object { $_ -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' })
}
function Invoke-OneVm([string]$Name, [scriptblock]$Activate, [int]$LockSeconds = 60) {
    $m = [Threading.Mutex]::new($false, 'Global\ludius-vm-activation')
    try {
        try { $got = $m.WaitOne([TimeSpan]::FromSeconds($LockSeconds)) }
        catch [Threading.AbandonedMutexException] { $got = $true }  # holder died; we own it now
        if (-not $got) { throw "one-VM cap: activation lock busy for $LockSeconds s; $Name not activated" }
        try {
            $exempt = Get-OneVmExemptIds
            $self = @(Get-VM -Name $Name -ErrorAction SilentlyContinue)
            if (-not ($self.Count -eq 1 -and "$($self[0].Id)" -in $exempt)) {
                $up = @(Get-VM -Name 'ludius-*' | Where-Object { $_.State -notin 'Off', 'Saved' -and $_.Name -ne $Name -and "$($_.Id)" -notin $exempt })
                if ($up) { throw "one-VM cap: $($up.Name -join ', ') is up; save it before activating $Name" }
            }
            & $Activate
        } finally { $m.ReleaseMutex() }
    } finally { $m.Dispose() }
}
