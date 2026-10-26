# Provisions the range DC made by new-dc.ps1 over PowerShell Direct: static address on the range switch, a new
# forest (AD DS + DNS), and the PDC as the range's authoritative NTP source on its own clock (no upstream peers,
# Hyper-V time sync off), so time-jump can set the range's time at the DC first (spec section 7).
# Ends with a "dc-clean" checkpoint. Re-runnable: finished steps are skipped.
param([string]$Domain = 'corp.ludius.test', [string]$NetBios = 'CORP', [string]$Address = '10.77.0.10', [switch]$NoCheckpoint)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_DC_VM
$secure = ConvertTo-SecureString $cfg.LUDIUS_DC_ADMIN_PASSWORD -AsPlainText -Force
$local = [pscredential]::new('Administrator', $secure)
$domainAdmin = [pscredential]::new("$NetBios\Administrator", $secure)

function Wait-Guest([pscredential[]]$creds, [int]$seconds = 1800) {
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $seconds) {
        foreach ($c in $creds) {
            try { Invoke-Command -VMName $vm -Credential $c { $true } -ErrorAction Stop | Out-Null; return $c } catch { }
        }
        Start-Sleep 10
    }
    throw "$vm not reachable over PowerShell Direct after $seconds s"
}

$cred = Wait-Guest @($domainAdmin, $local)
Get-VMDvdDrive $vm | Where-Object Path | Set-VMDvdDrive -Path $null

$isDc = Invoke-Command -VMName $vm -Credential $cred {
    param($address)
    $ErrorActionPreference = 'Stop'
    $nic = Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -First 1
    if (-not (Get-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $address -ErrorAction SilentlyContinue)) {
        Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled
        New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $address -PrefixLength 24 | Out-Null
        Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses 127.0.0.1
    }
    (Get-CimInstance Win32_ComputerSystem).DomainRole -ge 4
} -ArgumentList $Address

if (-not $isDc) {
    Invoke-Command -VMName $vm -Credential $cred {
        param($domain, $netbios, $dsrm)
        $ErrorActionPreference = 'Stop'
        Install-WindowsFeature AD-Domain-Services -IncludeManagementTools | Out-Null
        Install-ADDSForest -DomainName $domain -DomainNetbiosName $netbios -InstallDns -Force -NoRebootOnCompletion `
            -SafeModeAdministratorPassword (ConvertTo-SecureString $dsrm -AsPlainText -Force) -WarningAction SilentlyContinue | Out-Null
        Restart-Computer -Force
    } -ArgumentList $Domain, $NetBios, $cfg.LUDIUS_DC_DSRM_PASSWORD
    Start-Sleep 60
    $cred = Wait-Guest @($domainAdmin)
    # AD DS needs a moment after the first boot before its cmdlets answer.
    $t0 = Get-Date
    while (-not (Invoke-Command -VMName $vm -Credential $cred { try { [bool](Get-ADDomain) } catch { $false } })) {
        if (((Get-Date) - $t0).TotalSeconds -gt 600) { throw 'AD DS did not come up' }
        Start-Sleep 15
    }
}

Invoke-Command -VMName $vm -Credential $cred {
    $ErrorActionPreference = 'Stop'
    # Authoritative time from the local clock: no peers, announced as a reliable source to domain members.
    w32tm /config /syncfromflags:NO /reliable:YES /update | Out-Null
    $provider = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders'
    Set-ItemProperty "$provider\NtpServer" Enabled 1 -Type DWord
    Set-ItemProperty "$provider\VMICTimeProvider" Enabled 0 -Type DWord
    Restart-Service w32time
    Start-Sleep 5
    $d = Get-ADDomain
    "dc: $env:COMPUTERNAME domain $($d.DNSRoot) ($($d.NetBIOSName)) role $((Get-CimInstance Win32_ComputerSystem).DomainRole)"
    "time source: $((w32tm /query /source).Trim())"
    "address: " + ((Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -ne '127.0.0.1').IPAddress -join ',')
}

if (-not $NoCheckpoint) {
    Checkpoint-VM $vm -SnapshotName dc-clean
    Write-Output "checkpoint dc-clean taken"
}
