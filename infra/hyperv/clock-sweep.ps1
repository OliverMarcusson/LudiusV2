param(
    [string]$Locks = (Join-Path $PSScriptRoot '..\..\runs\locks'),
    [string]$SetClock = (Join-Path $PSScriptRoot 'set-clock.ps1'),
    [int]$StaleSeconds = 120
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Locks)) { return }
$now = [datetime]::UtcNow
foreach ($f in Get-ChildItem $Locks -Filter 'clock-*.json') {
    try { $lease = Get-Content -Raw $f.FullName | ConvertFrom-Json -DateKind String }
    catch { "lease $($f.Name): unreadable, left alone ($_)"; continue }
    $age = ($now - ([datetimeoffset]::Parse($lease.renewed)).UtcDateTime).TotalSeconds
    if ($age -le $StaleSeconds) { continue }
    try {
        & pwsh -NoProfile -NonInteractive -File $SetClock -Reset -VM $lease.vm | Out-Null
        if ($LASTEXITCODE) { throw "set-clock -Reset exit $LASTEXITCODE" }
    } catch { "lease $($f.Name): $($lease.vm) orphaned $([int]$age) s, reset FAILED, retrying next sweep: $_"; continue }
    if ((Get-Content -Raw $f.FullName | ConvertFrom-Json -DateKind String).renewed -eq $lease.renewed) { Remove-Item $f.FullName }
    "lease $($f.Name): $($lease.vm) orphaned $([int]$age) s (pid $($lease.pid)), range clocks reset"
}
