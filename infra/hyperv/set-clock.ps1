# Time-jump mode (spec section 7, test ranges only): set the guest clock to a session's logical time over
# PowerShell Direct, with Hyper-V time synchronization off so the host doesn't pull it back. Leaves
# clock-change events (4616) in the guest; never used for delivery. -Reset sets host time and turns time
# sync back on (re-enabling it alone doesn't pull a clock that is a day ahead back).
# Range server first (spec section 7): when .env names a running range server (LUDIUS_SRV_VM: Samba AD DC + chrony,
# the range's time source), its clock is set over SSH before the guest's, so a domain member that syncs from it
# follows. Its Hyper-V time sync stays off (new-srv.ps1); chrony has no upstream, so it keeps the jumped time.
param([string]$Time, [switch]$Reset, [string]$VM)  # -VM: another range member than .env LUDIUS_VM (the clock sweeper)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = if ($VM) { $VM } else { $cfg.LUDIUS_VM }
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $vm" }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$utc = if ($Reset) { [datetime]::UtcNow } else { $null }
$at = if ($Reset) { $null } else { [datetime]::ParseExact($Time, 's', [cultureinfo]::InvariantCulture) }  # guest-local
$set = {
    $t = if ($using:utc) { ([datetime]$using:utc).ToLocalTime() } else { $using:at }
    Set-Date -Date $t | Out-Null
    (Get-Date).ToString('s')
}
$srv = $cfg.LUDIUS_SRV_VM
if ($srv -and (Get-VM -Name $srv -ErrorAction SilentlyContinue).State -eq 'Running') {
    $ip = (Get-VMNetworkAdapter -VMName $srv | Where-Object SwitchName -eq 'Default Switch').IPAddresses | Where-Object { $_ -match '^\d+\.' } | Select-Object -First 1
    if (-not $ip) { throw "$srv reports no Default Switch address" }
    # Epoch seconds: the server's timezone matches the guest's, but an absolute instant needs no agreement on it.
    $epoch = if ($Reset) { [DateTimeOffset]::new($utc).ToUnixTimeSeconds() } else { [DateTimeOffset]::new($at).ToUnixTimeSeconds() }
    $key, $known = (Join-Path $PSScriptRoot 'build\srv\id_ed25519'), (Join-Path $PSScriptRoot 'build\srv\known_hosts')
    $dcTime = ssh -i $key -o "UserKnownHostsFile=$known" -o BatchMode=yes "ludius@$ip" "sudo date -s @$epoch +%Y-%m-%dT%H:%M:%S"
    if ($LASTEXITCODE) { throw "could not set the clock on $srv" }
    "dc $dcTime"
}
Disable-VMIntegrationService $vm -Name 'Time Synchronization'
$vmTime = Invoke-Command -VMName $vm -Credential $admin -ScriptBlock $set
$vmTime
# Bounded convergence: the guest must land within 10 s of the DC (both are set to the same instant).
if ($dcTime -and [math]::Abs(([datetime]$vmTime - [datetime]$dcTime).TotalSeconds) -gt 10) { throw "guest $vmTime vs DC ${dcTime}: more than 10 s apart" }
if ($Reset) { Enable-VMIntegrationService $vm -Name 'Time Synchronization' }
