param([string]$Address = '10.77.0.21', [string]$Mac = '00155D4C5521')
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $vm" }
if (-not $cfg.LUDIUS_SRV_ADDRESS) { throw 'no range server in .env (run new-srv.ps1)' }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
if (-not $cfg.LUDIUS_SRV_DC_ADMIN_PASSWORD) { throw 'no LUDIUS_SRV_DC_ADMIN_PASSWORD in .env (run provision-srv.ps1)' }
$domainAdmin = [pscredential]::new('CORP\Administrator', (ConvertTo-SecureString $cfg.LUDIUS_SRV_DC_ADMIN_PASSWORD -AsPlainText -Force))
$ca = Join-Path $PSScriptRoot 'build\srv\range-ca.crt'
if (-not (Test-Path $ca)) { throw "no range CA at $ca" }

if (-not (Get-VMNetworkAdapter -VMName $vm | Where-Object SwitchName -eq 'ludius-range')) {
    Add-VMNetworkAdapter -VMName $vm -SwitchName 'ludius-range' -StaticMacAddress $Mac
}
$mac = ((Get-VMNetworkAdapter -VMName $vm | Where-Object SwitchName -eq 'ludius-range').MacAddress -split '(..)' | Where-Object { $_ }) -join '-'

$s = New-PSSession -VMName $vm -Credential $admin
try {
    Copy-Item -ToSession $s $ca -Destination 'C:\Windows\Temp\range-ca.crt' -Force
    Invoke-Command $s {
        param($mac, $addr, $dns)
        $ErrorActionPreference = 'Stop'
        $nic = $null
        foreach ($i in 1..30) { $nic = Get-NetAdapter | Where-Object MacAddress -eq $mac; if ($nic) { break }; Start-Sleep 1 }
        if (-not $nic) { throw "range NIC $mac not visible in the guest" }
        if ($nic.Name -ne 'Range') { Rename-NetAdapter -InputObject $nic -NewName 'Range'; $nic = Get-NetAdapter -Name 'Range' }
        if (-not (Get-NetIPAddress -InterfaceAlias Range -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object IPAddress -eq $addr)) {
            Set-NetIPInterface -InterfaceAlias Range -Dhcp Disabled
            Get-NetIPAddress -InterfaceAlias Range -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
            New-NetIPAddress -InterfaceAlias Range -IPAddress $addr -PrefixLength 24 | Out-Null
        }
        Set-DnsClient -InterfaceAlias Range -RegisterThisConnectionsAddress $false
        Set-NetConnectionProfile -InterfaceAlias Range -NetworkCategory Private -ErrorAction SilentlyContinue
        if (-not (Get-DnsClientNrptRule | Where-Object Namespace -eq '.corp.ludius.test')) {
            Add-DnsClientNrptRule -Namespace '.corp.ludius.test', 'corp.ludius.test' -NameServers $dns | Out-Null
        }
        Clear-DnsClientCache
        Import-Certificate -FilePath 'C:\Windows\Temp\range-ca.crt' -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Remove-Item 'C:\Windows\Temp\range-ca.crt'

        $un = 'C:\Program Files\hMailServer\unins000.exe'
        if (Test-Path $un) {
            $p = Start-Process $un -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
            if ($p.ExitCode -ne 0) { throw "hMailServer uninstall failed: $($p.ExitCode)" }
        }
        $hosts = 'C:\Windows\System32\drivers\etc\hosts'
        $lines = Get-Content -LiteralPath $hosts
        $keep = $lines | Where-Object { $_ -notmatch '^\s*127\.0\.0\.1\s+mail\.corp\.ludius\.test\s*$' }
        if ($keep.Count -ne $lines.Count) { Set-Content -LiteralPath $hosts $keep -Encoding ASCII }

        $r = Resolve-DnsName mail.corp.ludius.test -Type A -DnsOnly
        $web = try { (Invoke-WebRequest https://autodiscover.corp.ludius.test/Autodiscover/Autodiscover.xml -UseBasicParsing).StatusCode } catch { [int]$_.Exception.Response.StatusCode }
        "Range $addr; mail.corp.ludius.test -> $($r.IPAddress -join ','); autodiscover HTTPS $web (401 = trusted TLS, needs login); hMailServer service: $([bool](Get-Service hMailServer -ErrorAction SilentlyContinue))"

        Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' DefaultDomainName $env:COMPUTERNAME
        if (-not (Get-CimInstance Win32_ComputerSystem).PartOfDomain) {
            Add-Computer -DomainName corp.ludius.test -Credential $using:domainAdmin
            'joined corp.ludius.test; restarting'
            Restart-Computer -Force
        }
    } -ArgumentList $mac, $Address, $cfg.LUDIUS_SRV_ADDRESS
}
finally { Remove-PSSession $s }
