$OneVmExemptIdFile = Join-Path $PSScriptRoot 'build\srv\vm-id'
function Get-OneVmExemptIds {
    if (-not (Test-Path -LiteralPath $OneVmExemptIdFile)) { return @() }
    @(Get-Content -LiteralPath $OneVmExemptIdFile | Where-Object { $_ -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' })
}
function Invoke-OneVm([string]$Name, [scriptblock]$Activate, [int]$Max = 1, [int]$LockSeconds = 60) {
    $m = [Threading.Mutex]::new($false, 'Global\ludius-vm-activation')
    try {
        try { $got = $m.WaitOne([TimeSpan]::FromSeconds($LockSeconds)) }
        catch [Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { throw "one-VM cap: activation lock busy for $LockSeconds s; $Name not activated" }
        try {
            $exempt = Get-OneVmExemptIds
            $self = @(Get-VM -Name $Name -ErrorAction SilentlyContinue)
            if (-not ($self.Count -eq 1 -and "$($self[0].Id)" -in $exempt)) {
                $up = @(Get-VM -Name 'ludius-*' | Where-Object { $_.State -notin 'Off', 'Saved' -and $_.Name -ne $Name -and "$($_.Id)" -notin $exempt })
                if ($up.Count -ge [Math]::Max(1, $Max)) { throw "one-VM cap ($Max): $($up.Name -join ', ') is up; save it before activating $Name" }
            }
            & $Activate
        } finally { $m.ReleaseMutex() }
    } finally { $m.Dispose() }
}
