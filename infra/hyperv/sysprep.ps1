param(
    [Parameter(Mandatory)][string]$VM,
    [Parameter(Mandatory)][string]$Confirm,
    [string]$Unattend
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }

if ($Confirm -ne $VM) { throw "sysprep needs -Confirm $VM (the VM name again)" }
if ($VM -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $VM" }
if ($VM -in $cfg.LUDIUS_VM, $cfg.LUDIUS_SRV_VM) { throw "$VM is the source test VM or the range server: never generalized" }
$state = (Get-VM -Name $VM).State
if ($state -ne 'Running') { throw "$VM is $state; start it yourself (one-VM cap) before generalizing" }
if ($Unattend -and -not (Test-Path $Unattend)) { throw "no answer file at $Unattend" }

$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$s = New-PSSession -VMName $VM -Credential $admin
try {
    $name = Invoke-Command $s { $env:COMPUTERNAME }
    if ($Unattend) { Copy-Item -ToSession $s $Unattend -Destination 'C:\Windows\Panther\unattend.xml' -Force }
    $code = Invoke-Command $s {
        param($answers)
        $a = @('/generalize', '/oobe', '/quit', '/quiet')
        if ($answers) { $a += '/unattend:C:\Windows\Panther\unattend.xml' }
        (Start-Process "$env:WINDIR\System32\Sysprep\sysprep.exe" -ArgumentList $a -Wait -PassThru).ExitCode
    } -ArgumentList ([bool]$Unattend)
    if ($code) { throw "sysprep exit $code; see C:\Windows\System32\Sysprep\Panther\setupact.log in $VM" }
    "$VM ($name) generalized; still running. Its next boot runs OOBE. Take a NEW checkpoint now if you want one."
}
finally { Remove-PSSession $s }
