# Range NTP check (test VM only): makes the Win10 test VM a temporary NTP client of DC01 on ludius-range,
# then jumps the DC +3 h, resyncs the member, and resets both to host time. Host and LAN time are untouched.
# It changes the member (range NIC, Hyper-V time sync off, w32tm peer): afterwards restore clean-watchdog
# and verify the VM has only its Default Switch NIC and time sync on (both checked to revert, 26 Oct 2026).
$ErrorActionPreference = 'Stop'
$root = 'C:\Users\Oliver\Documents\Development\LudiusV2'
$cfg=@{}; Get-Content "$root\.env" | % { if ($_ -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]]=$Matches[2] } }
$member = $cfg.LUDIUS_VM; $dc = $cfg.LUDIUS_DC_VM
$mc = [pscredential]::new($cfg.LUDIUS_ADMIN_USER, (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$dcc = [pscredential]::new('CORP\Administrator', (ConvertTo-SecureString $cfg.LUDIUS_DC_ADMIN_PASSWORD -AsPlainText -Force))
function Stamp($m) { "{0:HH:mm:ss} host | {1}" -f (Get-Date), $m }
function Clocks($tag) {
    $d = Invoke-Command -VMName $dc -Credential $dcc { [DateTime]::UtcNow }
    $m = Invoke-Command -VMName $member -Credential $mc { [DateTime]::UtcNow }
    $h = [DateTime]::UtcNow
    Stamp ("{0}: DC-host {1:N1} s, member-host {2:N1} s, member-DC {3:N1} s" -f $tag, ($d - $h).TotalSeconds, ($m - $h).TotalSeconds, ($m - $d).TotalSeconds)
}

# 1) Temporary range NIC on the member; its Hyper-V time sync off so only NTP moves its clock.
if (-not (Get-VMNetworkAdapter -VMName $member | Where-Object SwitchName -eq 'ludius-range')) {
    Add-VMNetworkAdapter -VMName $member -SwitchName 'ludius-range' -Name 'range'
}
$mac = (Get-VMNetworkAdapter -VMName $member | Where-Object SwitchName -eq 'ludius-range').MacAddress
Disable-VMIntegrationService -VMName $member -Name 'Time Synchronization'
Start-Sleep 5
Stamp "member range NIC MAC $mac; member time sync off"

# 2) Member: static range address, NTP client pointed at the DC only.
Invoke-Command -VMName $member -Credential $mc -ArgumentList $mac {
    param($mac)
    $ErrorActionPreference = 'Stop'
    $want = ($mac -replace '(..)(?!$)', '$1-')
    $nic = Get-NetAdapter | Where-Object { $_.MacAddress -eq $want }
    if (-not (Get-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress 10.77.0.21 -ErrorAction SilentlyContinue)) {
        Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled
        New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress 10.77.0.21 -PrefixLength 24 | Out-Null
    }
    Start-Sleep 5
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\VMICTimeProvider' Enabled 0 -Type DWord
    Set-Service w32time -StartupType Automatic; Start-Service w32time
    w32tm /config /manualpeerlist:10.77.0.10,0x8 /syncfromflags:manual /update | Out-Null
    Restart-Service w32time; Start-Sleep 3
    "member NIC $($nic.Name) 10.77.0.21"
    "UDP123 stripchart: " + ((w32tm /stripchart /computer:10.77.0.10 /samples:3 /dataonly) -join ' | ')
    w32tm /resync /force | Out-Null; Start-Sleep 3
    "member source after resync: " + (w32tm /query /source).Trim()
} | ForEach-Object { Stamp $_ }
Clocks 'synced'

# 3) DC first: jump the DC +3 h, then the member resyncs from it.
Invoke-Command -VMName $dc -Credential $dcc { Set-Date -Date (Get-Date).AddHours(3) | Out-Null }
Clocks 'DC jumped +3 h'
Invoke-Command -VMName $member -Credential $mc { w32tm /resync /force | Out-Null; Start-Sleep 3 }
Clocks 'member resynced'

# 4) Reset: DC back to host time, member resyncs.
$utc = [DateTime]::UtcNow
Invoke-Command -VMName $dc -Credential $dcc -ArgumentList $utc { param($u) Set-Date -Date ([DateTime]$u).ToLocalTime() | Out-Null }
Invoke-Command -VMName $member -Credential $mc { w32tm /resync /force | Out-Null; Start-Sleep 3 }
Clocks 'reset'
$events = Invoke-Command -VMName $member -Credential $mc { (Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4616; StartTime=(Get-Date).AddMinutes(-15)} -ErrorAction SilentlyContinue | Measure-Object).Count }
Stamp "member 4616 events (last 15 min): $events"
