# Provisions the test guest made by new-vm.ps1 over PowerShell Direct: range CA
# root, signed observer (uiAccess) under Program Files, logon task for the
# persona, and the slice of the golden-image roles a test guest needs (base,
# ux_noise). Ends with a "clean" checkpoint. Re-runnable.
# Stand-in for the Packer + Ansible build (spec section 7) on local Hyper-V.
param([switch]$NoUiAccess, [switch]$NoCheckpoint)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$installDir = 'C:\Program Files\RangeAssist'

Get-VMDvdDrive $vm | Where-Object Path | Set-VMDvdDrive -Path $null

# Range CA and code-signing certificate (spec: observer signed with the range's own CA).
$secrets = Join-Path $root 'secrets'
New-Item -ItemType Directory -Force $secrets | Out-Null
$ca = Get-ChildItem Cert:\CurrentUser\My | Where-Object Subject -eq 'CN=Ludius Range Root CA' | Select-Object -First 1
if (-not $ca) {
    $ca = New-SelfSignedCertificate -Subject 'CN=Ludius Range Root CA' -CertStoreLocation Cert:\CurrentUser\My -KeyUsage CertSign, CRLSign `
        -Type Custom -TextExtension @('2.5.29.19={critical}{text}ca=1&pathlength=0', '2.5.29.37={text}1.3.6.1.5.5.7.3.3') -NotAfter (Get-Date).AddYears(5)
}
$signer = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object Subject -eq 'CN=Ludius Range Code Signing' | Select-Object -First 1
if (-not $signer) {
    $signer = New-SelfSignedCertificate -Subject 'CN=Ludius Range Code Signing' -Type CodeSigningCert -Signer $ca `
        -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(2)
}
$caFile = Join-Path $secrets 'range-root-ca.cer'
Export-Certificate -Cert $ca -FilePath $caFile | Out-Null

$uiAccess = -not $NoUiAccess
$out = Join-Path $root 'out\observer'
dotnet publish (Join-Path $root 'guest\observer') -c Release -o $out -p:UiAccess=$uiAccess -nologo -v q
if ($LASTEXITCODE) { throw 'observer publish failed' }
$exe = Join-Path $out 'uiprobe.exe'
if ((Set-AuthenticodeSignature $exe -Certificate $signer -HashAlgorithm SHA256).Status -notin 'Valid', 'UnknownError') { throw 'signing failed' }

$s = New-PSSession -VMName $vm -Credential $admin
try {
    Invoke-Command $s { param($dir) Get-Process uiprobe -ErrorAction SilentlyContinue | Stop-Process -Force; New-Item -ItemType Directory -Force $dir | Out-Null } -ArgumentList $installDir
    Copy-Item -ToSession $s $exe, $caFile -Destination $installDir -Force
    Invoke-Command $s {
        param($dir, $persona, $personaPassword)
        $ErrorActionPreference = 'Stop'
        # Import-Certificate is denied over PowerShell Direct; certutil is not.
        certutil -f -addstore Root "$dir\range-root-ca.cer" | Out-Null
        if ($LASTEXITCODE) { throw "certutil failed: $LASTEXITCODE" }
        Remove-Item "$dir\range-root-ca.cer"

        # base: never sleep, Windows Update paused (updates belong in the image).
        powercfg /change standby-timeout-ac 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
        Set-Service wuauserv -StartupType Disabled; Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' | Out-Null
        Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1 -Type DWord
        # Autologon for the persona stays on (the unattend count runs out).
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty $wl AutoAdminLogon '1'; Set-ItemProperty $wl DefaultUserName $persona; Set-ItemProperty $wl DefaultPassword $personaPassword
        Remove-ItemProperty $wl AutoLogonCount -ErrorAction SilentlyContinue
        # No first-run tips or consumer content popping up over tasks.
        New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' | Out-Null
        Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1 -Type DWord
        Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableSoftLanding 1 -Type DWord

        # Observer: in the persona's interactive session at logon, never a service;
        # restarted daily and on failure (UIA client leaks).
        # uiAccess binaries only start through ShellExecute (AppInfo), not the
        # scheduler's CreateProcess (error 740), so explorer launches it.
        $action = New-ScheduledTaskAction -Execute 'C:\Windows\explorer.exe' -Argument "`"$dir\uiprobe.exe`""
        $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $persona), (New-ScheduledTaskTrigger -Daily -At 4am))
        $principal = New-ScheduledTaskPrincipal -UserId $persona -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName 'RangeAssist' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName 'RangeAssist'
        Start-Sleep 3
        $p = Get-Process uiprobe -ErrorAction SilentlyContinue
        if (-not $p) { throw "observer did not start (task result $((Get-ScheduledTaskInfo RangeAssist).LastTaskResult))" }
        "observer running, pid $($p.Id), session $($p.SessionId)"
    } -ArgumentList $installDir, $cfg.LUDIUS_PERSONA_USER, $cfg.LUDIUS_PERSONA_PASSWORD
}
finally { Remove-PSSession $s }

if (-not $NoCheckpoint) {
    Get-VMSnapshot $vm -Name clean -ErrorAction SilentlyContinue | Remove-VMSnapshot
    Checkpoint-VM $vm -SnapshotName clean
}
