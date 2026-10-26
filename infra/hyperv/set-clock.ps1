# Time-jump mode (spec section 7, test ranges only): set the guest clock to a session's logical time over
# PowerShell Direct, with Hyper-V time synchronization off so the host doesn't pull it back. Leaves
# clock-change events (4616) in the guest; never used for delivery. -Reset sets host time and turns time
# sync back on (re-enabling it alone doesn't pull a clock that is a day ahead back).
# Range DC first (spec section 7): when .env names a running range DC (LUDIUS_DC_VM), its clock is set before the
# guest's, so a range member that syncs from it follows. The DC's Hyper-V time sync stays off (it is the source).
param([string]$Time, [switch]$Reset)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$utc = if ($Reset) { [datetime]::UtcNow } else { $null }
$at = if ($Reset) { $null } else { [datetime]::ParseExact($Time, 's', [cultureinfo]::InvariantCulture) }  # guest-local
$set = {
    $t = if ($using:utc) { ([datetime]$using:utc).ToLocalTime() } else { $using:at }
    Set-Date -Date $t | Out-Null
    (Get-Date).ToString('s')
}
$dc = $cfg.LUDIUS_DC_VM
if ($dc -and (Get-VM -Name $dc -ErrorAction SilentlyContinue).State -eq 'Running') {
    $dcAdmin = [pscredential]::new('CORP\Administrator', (ConvertTo-SecureString $cfg.LUDIUS_DC_ADMIN_PASSWORD -AsPlainText -Force))
    $dcTime = Invoke-Command -VMName $dc -Credential $dcAdmin -ScriptBlock $set
    "dc $dcTime"
}
Disable-VMIntegrationService $vm -Name 'Time Synchronization'
$vmTime = Invoke-Command -VMName $vm -Credential $admin -ScriptBlock $set
$vmTime
# Bounded convergence: the guest must land within 10 s of the DC (both are set to the same instant).
if ($dcTime -and [math]::Abs(([datetime]$vmTime - [datetime]$dcTime).TotalSeconds) -gt 10) { throw "guest $vmTime vs DC ${dcTime}: more than 10 s apart" }
if ($Reset) { Enable-VMIntegrationService $vm -Name 'Time Synchronization' }
