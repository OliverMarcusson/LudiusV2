$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))

$deps = Join-Path $PSScriptRoot 'build\deps'
New-Item -ItemType Directory -Force $deps | Out-Null
$exe = Join-Path $deps 'Ledger.exe'
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" -nologo -target:winexe "-out:$exe" (Join-Path $root 'guest\drawnapp\Ledger.cs')
if ($LASTEXITCODE) { throw 'csc failed' }

$s = New-PSSession -VMName $cfg.LUDIUS_VM -Credential $admin
try {
    Copy-Item -ToSession $s $exe -Destination 'C:\Windows\Temp' -Force
    Invoke-Command $s {
        $ErrorActionPreference = 'Stop'
        $app = 'C:\Program Files\Ledger'
        New-Item -ItemType Directory -Force $app | Out-Null
        Copy-Item -Force 'C:\Windows\Temp\Ledger.exe' $app
        Remove-Item 'C:\Windows\Temp\Ledger.exe'
        icacls "$app\Ledger.exe" /reset /q | Out-Null
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut('C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Ledger.lnk')
        $link.TargetPath = "$app\Ledger.exe"
        $link.Save()
        "Ledger in Start"
    }
}
finally { Remove-PSSession $s }
