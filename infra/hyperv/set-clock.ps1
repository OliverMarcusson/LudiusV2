# Time-jump mode (spec section 7, test ranges only): set the guest clock to a session's logical time over
# PowerShell Direct, with Hyper-V time synchronization off so the host doesn't pull it back. Leaves
# clock-change events (4616) in the guest; never used for delivery. -Reset sets host time and turns time
# sync back on (re-enabling it alone doesn't pull a clock that is a day ahead back).
param([string]$Time, [switch]$Reset)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$utc = if ($Reset) { [datetime]::UtcNow } else { $null }
$at = if ($Reset) { $null } else { [datetime]::ParseExact($Time, 's', [cultureinfo]::InvariantCulture) }  # guest-local
Disable-VMIntegrationService $vm -Name 'Time Synchronization'
Invoke-Command -VMName $vm -Credential $admin -ScriptBlock {
    $t = if ($using:utc) { ([datetime]$using:utc).ToLocalTime() } else { $using:at }
    Set-Date -Date $t | Out-Null
    (Get-Date).ToString('s')
}
if ($Reset) { Enable-VMIntegrationService $vm -Name 'Time Synchronization' }
