# Creates the range's Linux infrastructure server on local Hyper-V (owner-approved 27 Oct 2026): Debian 13 from the
# official genericcloud image, configured by cloud-init. It will hold the Samba AD DC (domain, DNS, NTP) and grommunio
# (Exchange-protocol mail/calendar for Outlook), replacing the separate Windows DC and Office VMs; provision-srv.ps1
# does that. Exempt from the one-VM cap (one-vm.ps1). Two NICs: Default Switch (apt, host SSH) and ludius-range at
# a static address (the range clients). Hyper-V time sync stays on (real time); set-clock.ps1 turns it off for a jump.
#
# Image: fetch once into the ignored build folder and check it against Debian's SHA512SUMS:
#   https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.tar.xz -> build\srv\disk.raw
# Adds LUDIUS_SRV_* keys to .env and an SSH key under build\srv (both gitignored); leaves existing keys alone.
param(
    [string]$VMName = 'ludius-srv01',
    [string]$HostName = 'srv01',
    [string]$Address = '10.77.0.11/24',
    [string]$Switch = 'ludius-range'
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$build = Join-Path $PSScriptRoot 'build\srv'
$envFile = Join-Path $root '.env'
if (Get-VM $VMName -ErrorAction SilentlyContinue) { throw "$VMName already exists" }
$raw = Join-Path $build 'disk.raw'
$fixed = Join-Path $build 'disk.vhd'  # left by an earlier run that stopped after the footer step
if (-not (Test-Path $raw) -and -not (Test-Path $fixed)) { throw "no Debian image at $raw" }
if (-not (Get-VMSwitch -Name $Switch -ErrorAction SilentlyContinue)) { throw "no Hyper-V switch $Switch (create the private range switch first)" }
$vhd = Join-Path (Get-VMHost).VirtualHardDiskPath "$VMName.vhdx"
# The source disk.vhd is only deleted once the VM is up, so a leftover VHDX is safe to delete before a rerun.
if (Test-Path $vhd) { throw "$vhd is left over from a failed run (no VM $VMName); delete it and rerun" }

function New-Password {
    $chars = [char[]]'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    'Lr7-' + -join (1..16 | ForEach-Object { $chars[[Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)] })
}
$values = [ordered]@{}
foreach ($line in Get-Content $envFile) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] } }
$added = [ordered]@{ LUDIUS_SRV_VM = $VMName; LUDIUS_SRV_ADDRESS = ($Address -split '/')[0]; LUDIUS_SRV_PASSWORD = New-Password }
# Re-run after a failed creation: keep the values already written (the VM itself does not exist, checked above).
foreach ($k in @($added.Keys)) { if ($values.Contains($k)) { $added[$k] = $values[$k] } else { "$k=$($added[$k])" | Add-Content $envFile } }

$key = Join-Path $build 'id_ed25519'
if (-not (Test-Path $key)) { ssh-keygen -q -t ed25519 -N '' -C "ludius@$HostName" -f $key; if ($LASTEXITCODE) { throw 'ssh-keygen failed' } }
$pub = (Get-Content "$key.pub" -Raw).Trim()

# Fixed MACs, so the guest's network config can match the NICs whatever the kernel calls them.
$macNat, $macRange = '00155D4C5501', '00155D4C5502'
$fmt = { param($m) ($m -split '(..)' | Where-Object { $_ }) -join ':' }
@"
instance-id: $VMName-1
local-hostname: $HostName
"@ | Set-Content -Encoding ascii (Join-Path $build 'meta-data')
@"
#cloud-config
hostname: $HostName
timezone: Europe/Stockholm
users:
  - name: ludius
    groups: [sudo]
    shell: /bin/bash
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    lock_passwd: false
    ssh_authorized_keys: ['$pub']
chpasswd:
  expire: false
  users: [{name: ludius, password: '$($added.LUDIUS_SRV_PASSWORD)', type: text}]
ssh_pwauth: false
package_update: true
packages: [hyperv-daemons]
"@ | Set-Content -Encoding ascii (Join-Path $build 'user-data')
@"
version: 2
ethernets:
  nat:
    match: {macaddress: '$(& $fmt $macNat)'}
    dhcp4: true
  range:
    match: {macaddress: '$(& $fmt $macRange)'}
    addresses: [$Address]
"@ | Set-Content -Encoding ascii (Join-Path $build 'network-config')
$seed = Join-Path $build 'seed.iso'
uv run --quiet --with pycdlib python (Join-Path $PSScriptRoot 'make-seed.py') $seed (Join-Path $build 'user-data') (Join-Path $build 'meta-data') (Join-Path $build 'network-config')
if ($LASTEXITCODE) { throw 'seed ISO build failed' }

# raw -> fixed VHD (the raw bytes plus a 512-byte footer, written in place) -> dynamic VHDX.
if (Test-Path $raw) {
$len = (Get-Item $raw).Length
if ($len % 1MB) { throw 'raw image is not a whole number of MiB' }
$f = [byte[]]::new(512)
$put = { param($o, [byte[]]$b) [Array]::Copy($b, 0, $f, $o, $b.Length) }
$be = { param([UInt64]$v, $n) $b = [BitConverter]::GetBytes($v); [Array]::Reverse($b); $b[(8 - $n)..7] }
& $put 0 ([Text.Encoding]::ASCII.GetBytes('conectix'))
& $put 8 (& $be 2 4); & $put 12 (& $be 0x10000 4); & $put 16 (& $be ([UInt64]::MaxValue) 8)
& $put 28 ([Text.Encoding]::ASCII.GetBytes('win ')); & $put 32 (& $be 0x000A0000 4); & $put 36 ([Text.Encoding]::ASCII.GetBytes('Wi2k'))
& $put 40 (& $be $len 8); & $put 48 (& $be $len 8)
$sectors = [Math]::Min($len / 512, 65535 * 16 * 255)  # CHS geometry per the VHD spec
if ($sectors -ge 65535 * 16 * 63) { $spt = 255; $heads = 16; $cth = $sectors / $spt }
else { $spt = 17; $cth = $sectors / $spt; $heads = [Math]::Max([Math]::Ceiling(($cth + 1023) / 1024), 4); if ($cth -ge $heads * 1024 -or $heads -gt 16) { $spt = 31; $heads = 16; $cth = $sectors / $spt }; if ($cth -ge $heads * 1024) { $spt = 63; $heads = 16; $cth = $sectors / $spt } }
& $put 56 (& $be ([Math]::Floor($cth / $heads)) 2); $f[58] = [byte]$heads; $f[59] = [byte]$spt
& $put 60 (& $be 2 4)  # fixed
& $put 68 ([guid]::NewGuid().ToByteArray())
$sum = 0; foreach ($b in $f) { $sum += $b }
& $put 64 (& $be ((-bnot [UInt32]$sum) -band 0xFFFFFFFF) 4)
$fs = [IO.File]::Open($raw, 'Open', 'ReadWrite'); try { $fs.Seek(0, 'End') | Out-Null; $fs.Write($f, 0, 512) } finally { $fs.Close() }
Move-Item $raw $fixed
}
fsutil sparse setflag $fixed 0 | Out-Null  # tar extracts the image sparse; Hyper-V refuses sparse disks
if ($LASTEXITCODE) { throw "could not clear the sparse flag on $fixed" }
Convert-VHD -Path $fixed -DestinationPath $vhd -VHDType Dynamic
Resize-VHD -Path $vhd -SizeBytes 24GB  # cloud-init grows the root partition at first boot

$new = New-VM -Name $VMName -Generation 2 -MemoryStartupBytes 3GB -VHDPath $vhd -SwitchName 'Default Switch'
Set-Content -Encoding ascii (Join-Path $build 'vm-id') "$($new.Id)"  # the one-VM cap exempts this Id, not the name
Set-VM $VMName -ProcessorCount 2 -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Standard
Set-VMNetworkAdapter -VMName $VMName -StaticMacAddress $macNat
Add-VMNetworkAdapter -VMName $VMName -SwitchName $Switch -StaticMacAddress $macRange
Set-VMFirmware $VMName -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority
Add-VMDvdDrive $VMName -Path $seed
Set-VMFirmware $VMName -FirstBootDevice (Get-VMHardDiskDrive $VMName)
Start-VM $VMName  # exempt from the one-VM cap
Remove-Item $fixed
Write-Output "Started $VMName; cloud-init configures user ludius (key build\srv\id_ed25519), range address $Address"
