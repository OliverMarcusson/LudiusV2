# Creates an unattended Windows 10 Pro test guest on local Hyper-V.
# Needs Hyper-V Administrators membership, not elevation. Accounts and passwords
# go to the repo's .env (gitignored); the filled answer file only to build/.
param(
    [string]$VMName = 'ludius-win10-01',
    [string]$Iso = "$env:USERPROFILE\Downloads\win10.iso",
    [string]$ComputerName = 'WS-ABERG01',
    [string]$PersonaUser = 'aberg',
    [string]$PersonaName = 'Anna Berg',
    [string]$AdminUser = 'rangeadmin',
    [int]$Width = 1920,
    [int]$Height = 1080
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$build = Join-Path $PSScriptRoot 'build'
$envFile = Join-Path $root '.env'
if (Get-VM $VMName -ErrorAction SilentlyContinue) { throw "$VMName already exists" }
# Owner's cap (one-vm.ps1): fail early, before minutes of creation; the start below rechecks under the lock.
$running = @(Get-VM -Name 'ludius-*' | Where-Object { $_.State -notin 'Off', 'Saved' })
if ($running) { throw "one-VM cap: $($running.Name -join ', ') is running; save it first (Save-VM)" }

function New-Password {
    $chars = [char[]]'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    'Lr7-' + -join (1..16 | ForEach-Object { $chars[[Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)] })
}
$values = [ordered]@{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] } }
}
$values['LUDIUS_VM'] = $VMName
$values['LUDIUS_ADMIN_USER'] = $AdminUser
$values['LUDIUS_ADMIN_PASSWORD'] = New-Password
$values['LUDIUS_PERSONA_USER'] = $PersonaUser
$values['LUDIUS_PERSONA_PASSWORD'] = New-Password
$values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } | Set-Content $envFile

New-Item -ItemType Directory -Force $build | Out-Null
$xml = (Get-Content -Raw (Join-Path $PSScriptRoot 'autounattend.xml')).
    Replace('{{COMPUTER_NAME}}', $ComputerName).Replace('{{ADMIN_USER}}', $AdminUser).
    Replace('{{ADMIN_PASSWORD}}', $values['LUDIUS_ADMIN_PASSWORD']).Replace('{{PERSONA_USER}}', $PersonaUser).
    Replace('{{PERSONA_NAME}}', $PersonaName).Replace('{{PERSONA_PASSWORD}}', $values['LUDIUS_PERSONA_PASSWORD'])
Set-Content -Encoding utf8 (Join-Path $build 'autounattend.xml') $xml
$answerIso = Join-Path $build 'answer.iso'
uv run --quiet --with pycdlib python (Join-Path $PSScriptRoot 'make-iso.py') (Join-Path $build 'autounattend.xml') $answerIso
if ($LASTEXITCODE) { throw 'answer ISO build failed' }

$vhd = Join-Path (Get-VMHost).VirtualHardDiskPath "$VMName.vhdx"
New-VM -Name $VMName -Generation 2 -MemoryStartupBytes 6GB -NewVHDPath $vhd -NewVHDSizeBytes 64GB -SwitchName 'Default Switch' | Out-Null
Set-VM $VMName -ProcessorCount 4 -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Standard
# Fixed resolution lives in VM config, not the guest (spec section 4).
Set-VMVideo $VMName -ResolutionType Single -HorizontalResolution $Width -VerticalResolution $Height
Add-VMDvdDrive $VMName -Path $Iso
Add-VMDvdDrive $VMName -Path $answerIso
Set-VMFirmware $VMName -FirstBootDevice (Get-VMDvdDrive $VMName | Where-Object Path -eq $Iso)
Enable-VMIntegrationService $VMName -Name 'Guest Service Interface'
. (Join-Path $PSScriptRoot 'one-vm.ps1')
Invoke-OneVm $VMName { Start-VM $VMName }  # recheck under the host-wide lock: creation took minutes

# The DVD boot loader waits for "press any key"; answer it through the same
# synthetic keyboard the host agent uses.
$cs = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem -Filter "ElementName='$VMName'"
$kbd = Get-CimAssociatedInstance $cs -ResultClassName Msvm_Keyboard
foreach ($i in 1..20) { Invoke-CimMethod -InputObject $kbd -MethodName TypeKey -Arguments @{ keyCode = [uint32]0x0D } -ErrorAction SilentlyContinue | Out-Null; Start-Sleep -Milliseconds 500 }
Write-Output "Started $VMName; unattended install running (persona $PersonaUser, admin $AdminUser, credentials in .env)"
