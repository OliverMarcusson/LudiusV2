# Range mail on the ONE test guest (spec sections 2/5: inbox, mail threads) over PowerShell Direct, so mail needs no
# separate server VM: hMailServer bound to 127.0.0.1 only, domain $LUDIUS_MAIL_DOMAIN (corp.ludius.test), one mailbox
# per narrative person (personas and colleagues), no delivery to any other domain. mail.<domain> resolves to
# 127.0.0.1 through the guest hosts file; Outlook connects there over IMAP 143 / SMTP 25. Re-runnable; leaves
# checkpoints alone. Run provision.ps1 first.
#
# Server: hMailServer 5.7.2-B3124 x64 (AGPL), official GitHub release; unsigned, so the SHA256 is pinned to the
# release asset digest. Fetch it once into the ignored build folder:
#   infra\hyperv\build\deps\hMailServer-5.7.2-B3124-x64.exe
#   https://github.com/hmailserver/hmailserver/releases/download/hMailServer-5.7.2-b3124/hMailServer-5.7.2-B3124-x64.exe
# .env: LUDIUS_MAIL_DOMAIN, LUDIUS_MAIL_ADMIN_PASSWORD (hMailServer Administrator), LUDIUS_MAIL_PASSWORD (mailboxes).
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $vm" }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
foreach ($k in 'LUDIUS_MAIL_DOMAIN', 'LUDIUS_MAIL_ADMIN_PASSWORD', 'LUDIUS_MAIL_PASSWORD') { if (-not $cfg[$k]) { throw "$k missing in .env" } }

$exe = Join-Path $PSScriptRoot 'build\deps\hMailServer-5.7.2-B3124-x64.exe'
$exeSha256 = '9555c19eb0ff1659cd2aff8b2c716e202347639c7bbe2b0f72ee0e27979d72fa'
if ((Get-FileHash $exe -Algorithm SHA256).Hash -ne $exeSha256) { throw "$exe missing or not the pinned hMailServer 5.7.2-B3124" }

# Mailboxes: everyone in the company narrative ("- name: Full Name" followed by "user: account").
$people = @()
$name = $null
foreach ($line in Get-Content (Join-Path $root 'runs\world\narrative.yaml')) {
    if ($line -match '^- name:\s*(.+?)\s*$') { $name = $Matches[1] }
    elseif ($name -and $line -match '^\s+user:\s*([a-z0-9]+)\s*$') { $people += "$($Matches[1])|$name"; $name = $null }
}
if ($people.Count -lt 2) { throw 'no people found in runs\world\narrative.yaml' }

$s = New-PSSession -VMName $vm -Credential $admin
try {
    Copy-Item -ToSession $s $exe -Destination 'C:\Windows\Temp' -Force
    Invoke-Command $s {
        param($setup, $domain, $adminPw, $boxPw, $people)
        $ErrorActionPreference = 'Stop'
        if (-not (Get-Service hMailServer -ErrorAction SilentlyContinue)) {
            $p = Start-Process "C:\Windows\Temp\$setup" -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/ADMINPASSWORD=$adminPw"
            if ($p.ExitCode -ne 0) { throw "hMailServer setup failed: $($p.ExitCode)" }
        }
        Remove-Item "C:\Windows\Temp\$setup"
        if ((Get-Service hMailServer).Status -ne 'Running') { Start-Service hMailServer }

        $app = New-Object -ComObject hMailServer.Application
        if (-not $app.Authenticate('Administrator', $adminPw)) { throw 'hMailServer Administrator password rejected' }
        $set = $app.Settings
        $set.HostName = "mail.$domain"
        # Loopback only, SMTP and IMAP only: nothing on the guest's network interfaces.
        $ports = $set.TCPIPPorts
        for ($i = $ports.Count - 1; $i -ge 0; $i--) {
            $port = $ports.Item($i)
            if ($port.PortNumber -in 25, 143) { $port.Address = '127.0.0.1'; $port.Save() } else { $ports.DeleteByDBID($port.ID) }
        }
        # Local-to-local only: an address outside the range domain is refused, never relayed or looked up.
        foreach ($r in @(for ($i = 0; $i -lt $set.SecurityRanges.Count; $i++) { $set.SecurityRanges.Item($i) })) {
            $r.AllowDeliveryFromLocalToRemote = $false
            $r.AllowDeliveryFromRemoteToRemote = $false
            $r.Save()
        }
        $set.SMTPRelayer = ''

        $d = $null
        try { $d = $app.Domains.ItemByName($domain) } catch { }
        if (-not $d) { $d = $app.Domains.Add(); $d.Name = $domain; $d.Active = $true; $d.Save() }
        foreach ($entry in $people) {
            $user, $full = $entry -split '\|', 2
            $addr = "$user@$domain"
            $a = $null
            try { $a = $d.Accounts.ItemByAddress($addr) } catch { }
            if (-not $a) { $a = $d.Accounts.Add(); $a.Address = $addr }
            $a.Password = $boxPw  # re-set each run, so .env stays the source of truth
            $first, $last = $full -split ' ', 2
            $a.PersonFirstName = $first; $a.PersonLastName = "$last"
            $a.Active = $true; $a.MaxSize = 500
            $a.Save()
        }
        $app.Stop(); $app.Start()

        $hosts = 'C:\Windows\System32\drivers\etc\hosts'
        if (-not (Select-String -LiteralPath $hosts -Pattern "\smail\.$([regex]::Escape($domain))\s*$" -Quiet)) {
            Add-Content -LiteralPath $hosts "`r`n127.0.0.1`tmail.$domain" -Encoding ASCII
        }
        $listen = Get-NetTCPConnection -State Listen -OwningProcess (Get-Process hMailServer).Id | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" }
        "hMailServer $($app.Version), $($d.Accounts.Count) mailboxes @$domain, listening $($listen -join ' ')"
    } -ArgumentList (Split-Path $exe -Leaf), $cfg.LUDIUS_MAIL_DOMAIN, $cfg.LUDIUS_MAIL_ADMIN_PASSWORD, $cfg.LUDIUS_MAIL_PASSWORD, $people
}
finally { Remove-PSSession $s }
