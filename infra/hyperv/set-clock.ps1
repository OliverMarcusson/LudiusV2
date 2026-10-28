# Time-jump mode (spec section 7, test ranges only): set the guest clock to a session's logical time over
# PowerShell Direct, with Hyper-V time synchronization off so the host doesn't pull it back. Leaves
# clock-change events (4616) in the guest; never used for delivery. -Reset sets host time and turns time
# sync back on (re-enabling it alone doesn't pull a clock that is a day ahead back).
# Range server first (spec section 7): when .env names a range server (LUDIUS_SRV_VM: Samba AD DC + chrony, the
# range's time source), a jump sets its clock over SSH before the guest's, with its Hyper-V time sync off like the
# guest's; chrony has no upstream, so it holds the jumped time. All or nothing: if the guest step fails, the server is
# put back on real time before the error is raised (a DC/member split over 5 min breaks Kerberos). While the server
# holds a jumped clock, build\srv\clock-jumped says so; -Reset fails (nonzero, so the clock lease is kept) if that
# server can't be reset, e.g. it is saved. -Reset always tries both machines and turns both time syncs back on.
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
$marker = Join-Path $PSScriptRoot 'build\srv\clock-jumped'
function Set-SrvClock([datetime]$instant) {
    if ((Get-VM -Name $srv).State -ne 'Running') { throw "$srv is not running" }
    $ip = (Get-VMNetworkAdapter -VMName $srv | Where-Object SwitchName -eq 'Default Switch').IPAddresses | Where-Object { $_ -match '^\d+\.' } | Select-Object -First 1
    if (-not $ip) { throw "$srv reports no Default Switch address" }
    # The host key is pinned by name (build\srv\known_hosts "ludius-srv01"): the Default Switch address changes when
    # the host reboots.
    # Epoch seconds: the server's timezone matches the guest's, but an absolute instant needs no agreement on it.
    $epoch = [DateTimeOffset]::new($instant).ToUnixTimeSeconds()
    $key, $known = (Join-Path $PSScriptRoot 'build\srv\id_ed25519'), (Join-Path $PSScriptRoot 'build\srv\known_hosts')
    $t = ssh -i $key -o "UserKnownHostsFile=$known" -o HostKeyAlias=ludius-srv01 -o BatchMode=yes "ludius@$ip" "sudo date -s @$epoch +%Y-%m-%dT%H:%M:%S"
    if ($LASTEXITCODE) { throw "could not set the clock on $srv" }
    $t
}
function Reset-Srv {
    $t = Set-SrvClock ([datetime]::UtcNow)
    Enable-VMIntegrationService $srv -Name 'Time Synchronization'
    Remove-Item -LiteralPath $marker -ErrorAction SilentlyContinue
    "dc $t"
}

if ($Reset) {
    $failed = @()
    if ($srv -and (Get-VM -Name $srv -ErrorAction SilentlyContinue)) {
        if ((Get-VM -Name $srv).State -eq 'Running') { try { Reset-Srv } catch { $failed += "$srv`: $_" } }
        elseif (Test-Path -LiteralPath $marker) { $failed += "$srv holds a jumped clock but is not running; start it and reset again" }
        # Always back on, whatever failed above: real time on its next start, and the only recovery left if SSH failed.
        try { Enable-VMIntegrationService $srv -Name 'Time Synchronization' } catch { $failed += "$srv time sync: $_" }
    }
    try { Invoke-Command -VMName $vm -Credential $admin -ScriptBlock $set } catch { $failed += "$vm`: $_" }
    try { Enable-VMIntegrationService $vm -Name 'Time Synchronization' } catch { $failed += "$vm time sync: $_" }
    if ($failed) { throw "reset incomplete: $($failed -join '; ')" }
    return
}

$dcTime = $null
if ($srv -and (Get-VM -Name $srv -ErrorAction SilentlyContinue).State -eq 'Running') {
    Disable-VMIntegrationService $srv -Name 'Time Synchronization'
    New-Item -ItemType File -Force -Path $marker | Out-Null  # before the jump: a crash mid-jump still leaves it
    try { $dcTime = Set-SrvClock $at; "dc $dcTime" }
    catch { $e = $_; try { Reset-Srv | Out-Null } catch { throw "$e; and $srv could not be put back on real time: $_" }; throw $e }
}
try {
    Disable-VMIntegrationService $vm -Name 'Time Synchronization'
    $vmTime = Invoke-Command -VMName $vm -Credential $admin -ScriptBlock $set
    $vmTime
    # Bounded convergence: the guest must land within 10 s of the DC (both are set to the same instant).
    if ($dcTime -and [math]::Abs(([datetime]$vmTime - [datetime]$dcTime).TotalSeconds) -gt 10) { throw "guest $vmTime vs DC ${dcTime}: more than 10 s apart" }
}
catch {
    # The guest may already be jumped (or diverged): put both back on real time, or the DC/member split stays.
    $e, $also = $_, @()
    try {
        Invoke-Command -VMName $vm -Credential $admin -ArgumentList ([datetime]::UtcNow.ToString('o')) -ScriptBlock {
            param($u) Set-Date -Date ([datetime]::Parse($u, $null, 'RoundtripKind').ToLocalTime()) | Out-Null
        }
        Enable-VMIntegrationService $vm -Name 'Time Synchronization'
    } catch { $also += "$vm could not be put back on real time: $_" }
    if ($dcTime) { try { Reset-Srv | Out-Null } catch { $also += "$srv could not be put back on real time: $_" } }
    if ($also) { throw "$e; and $($also -join '; ')" }
    throw $e
}
