# Creates the range's domain controller and NTP source on local Hyper-V: Windows Server 2025 Standard Evaluation
# (Server Core) from Microsoft's evaluation ISO, unattended. It sits on a Private switch (ludius-range): VMs only,
# no host or LAN access; PowerShell Direct still reaches it over VMBus. provision-dc.ps1 does the rest.
# Adds LUDIUS_DC_* keys to .env (gitignored) and leaves every existing key as it is.
param(
    [string]$VMName = 'ludius-dc01',
    [string]$Iso = (Join-Path $PSScriptRoot 'build\media\WinServer2025-eval-en-us.iso'),
    [string]$ComputerName = 'DC01',
    [string]$Switch = 'ludius-range'
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$build = Join-Path $PSScriptRoot 'build'
$envFile = Join-Path $root '.env'
if (Get-VM $VMName -ErrorAction SilentlyContinue) { throw "$VMName already exists" }
# Owner's cap (one-vm.ps1): fail early, before minutes of creation; the start below rechecks under the lock.
$running = @(Get-VM -Name 'ludius-*' | Where-Object { $_.State -notin 'Off', 'Saved' })
if ($running) { throw "one-VM cap: $($running.Name -join ', ') is running; save it first (Save-VM)" }
if (-not (Test-Path $Iso)) { throw "no Server ISO at $Iso" }

function New-Password {
    $chars = [char[]]'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    'Lr7-' + -join (1..16 | ForEach-Object { $chars[[Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)] })
}
$values = [ordered]@{}
foreach ($line in Get-Content $envFile) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] } }
$added = [ordered]@{ LUDIUS_DC_VM = $VMName; LUDIUS_DC_ADMIN_PASSWORD = New-Password; LUDIUS_DC_DSRM_PASSWORD = New-Password }
foreach ($k in $added.Keys) { if ($values.Contains($k)) { throw "$k is already in .env; remove the old DC first" } }
$added.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } | Add-Content $envFile

if (-not (Get-VMSwitch $Switch -ErrorAction SilentlyContinue)) { New-VMSwitch -Name $Switch -SwitchType Private | Out-Null }

New-Item -ItemType Directory -Force $build | Out-Null
$xml = (Get-Content -Raw (Join-Path $PSScriptRoot 'autounattend-server.xml')).
    Replace('{{COMPUTER_NAME}}', $ComputerName).Replace('{{ADMIN_PASSWORD}}', $added.LUDIUS_DC_ADMIN_PASSWORD)
$answerXml = Join-Path $build 'dc\autounattend.xml'
New-Item -ItemType Directory -Force (Split-Path $answerXml) | Out-Null
Set-Content -Encoding utf8 $answerXml $xml
$answerIso = Join-Path $build 'dc\answer.iso'
uv run --quiet --with pycdlib python (Join-Path $PSScriptRoot 'make-iso.py') $answerXml $answerIso
if ($LASTEXITCODE) { throw 'answer ISO build failed' }

$vhd = Join-Path (Get-VMHost).VirtualHardDiskPath "$VMName.vhdx"
New-VM -Name $VMName -Generation 2 -MemoryStartupBytes 2GB -NewVHDPath $vhd -NewVHDSizeBytes 40GB -SwitchName $Switch | Out-Null
Set-VM $VMName -ProcessorCount 2 -DynamicMemory -MemoryMinimumBytes 1GB -MemoryMaximumBytes 3GB -AutomaticCheckpointsEnabled $false -CheckpointType Standard
Add-VMDvdDrive $VMName -Path $Iso
Add-VMDvdDrive $VMName -Path $answerIso
Set-VMFirmware $VMName -FirstBootDevice (Get-VMDvdDrive $VMName | Where-Object Path -eq $Iso)
Enable-VMIntegrationService $VMName -Name 'Guest Service Interface'
# The DC is the range's time source: its clock is set directly (time-jump), never pulled from the host.
Disable-VMIntegrationService $VMName -Name 'Time Synchronization'
. (Join-Path $PSScriptRoot 'one-vm.ps1')
Invoke-OneVm $VMName { Start-VM $VMName }  # recheck under the host-wide lock: creation took minutes

# "Press any key to boot from CD or DVD": answer it through the synthetic keyboard.
$cs = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem -Filter "ElementName='$VMName'"
$kbd = Get-CimAssociatedInstance $cs -ResultClassName Msvm_Keyboard
foreach ($i in 1..20) { Invoke-CimMethod -InputObject $kbd -MethodName TypeKey -Arguments @{ keyCode = [uint32]0x0D } -ErrorAction SilentlyContinue | Out-Null; Start-Sleep -Milliseconds 500 }
Write-Output "Started $VMName on $Switch; unattended Server Core install running (Administrator password in .env)"
