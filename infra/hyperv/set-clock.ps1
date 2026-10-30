param([string]$Time, [switch]$Reset, [string]$VM)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = if ($VM) { $VM } else { $cfg.LUDIUS_VM }
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $vm" }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$utc = if ($Reset) { [datetime]::UtcNow } else { $null }
$at = if ($Reset) { $null } else { [datetime]::ParseExact($Time, 's', [cultureinfo]::InvariantCulture) }
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
    New-Item -ItemType File -Force -Path $marker | Out-Null
    try { $dcTime = Set-SrvClock $at; "dc $dcTime" }
    catch { $e = $_; try { Reset-Srv | Out-Null } catch { throw "$e; and $srv could not be put back on real time: $_" }; throw $e }
}
try {
    Disable-VMIntegrationService $vm -Name 'Time Synchronization'
    $vmTime = Invoke-Command -VMName $vm -Credential $admin -ScriptBlock $set
    $vmTime
    if ($dcTime -and [math]::Abs(([datetime]$vmTime - [datetime]$dcTime).TotalSeconds) -gt 10) { throw "guest $vmTime vs DC ${dcTime}: more than 10 s apart" }
}
catch {
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
