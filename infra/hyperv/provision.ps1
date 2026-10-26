# Provisions the test guest made by new-vm.ps1 over PowerShell Direct: range CA
# root, signed observer (uiAccess) under Program Files, logon task for the
# persona, and the slice of the golden-image roles a test guest needs (base,
# eventlogs, ux_noise). Ends with a new checkpoint (-Checkpoint, default "clean"); an
# existing checkpoint is never replaced, so re-runs pass -NoCheckpoint or a new name.
# Stand-in for the Packer + Ansible build (spec section 7) on local Hyper-V.
param([switch]$NoUiAccess, [switch]$NoCheckpoint, [string]$Checkpoint = 'clean')
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$installDir = 'C:\Program Files\RangeAssist'
if (-not $NoCheckpoint -and (Get-VMSnapshot $vm -Name $Checkpoint -ErrorAction SilentlyContinue)) {
    throw "checkpoint '$Checkpoint' exists and is never replaced: pass -NoCheckpoint or -Checkpoint <new name>"
}

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
        # eventlogs: large enough that a scenario's events survive a day of noise (Sysmon's log: not installed yet).
        foreach ($l in @{ Security = 1GB; System = 256MB; Application = 256MB }.GetEnumerator()) {
            wevtutil sl $l.Key /ms:$($l.Value); if ($LASTEXITCODE) { throw "wevtutil $($l.Key): $LASTEXITCODE" }
        }
        # ux_noise, in the persona's hive: no minimize/taskbar animations, 100% scaling (96 dpi), no toasts over
        # tasks (focus assist's effect). Read at logon: they apply from the persona's next logon.
        $sid = ([Security.Principal.NTAccount]$persona).Translate([Security.Principal.SecurityIdentifier]).Value
        $loaded = Test-Path "Registry::HKEY_USERS\$sid"
        $hive = if ($loaded) { "Registry::HKEY_USERS\$sid" } else { 'Registry::HKEY_USERS\LudiusPersona' }
        if (-not $loaded) { reg load 'HKU\LudiusPersona' "C:\Users\$persona\NTUSER.DAT" | Out-Null; if ($LASTEXITCODE) { throw 'reg load failed' } }
        try {
            $adv = "$hive\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
            $push = "$hive\Software\Microsoft\Windows\CurrentVersion\PushNotifications"
            foreach ($k in "$hive\Control Panel\Desktop\WindowMetrics", $adv, $push) { if (-not (Test-Path $k)) { New-Item $k | Out-Null } }
            Set-ItemProperty "$hive\Control Panel\Desktop\WindowMetrics" MinAnimate '0'
            Set-ItemProperty $adv TaskbarAnimations 0 -Type DWord
            Set-ItemProperty "$hive\Control Panel\Desktop" LogPixels 96 -Type DWord
            Set-ItemProperty "$hive\Control Panel\Desktop" Win8DpiScaling 1 -Type DWord
            Set-ItemProperty $push ToastEnabled 0 -Type DWord
        } finally {
            if (-not $loaded) { [gc]::Collect(); reg unload 'HKU\LudiusPersona' | Out-Null }
        }
        # Autologon for the persona stays on (the unattend count runs out).
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty $wl AutoAdminLogon '1'; Set-ItemProperty $wl DefaultUserName $persona; Set-ItemProperty $wl DefaultPassword $personaPassword
        Remove-ItemProperty $wl AutoLogonCount -ErrorAction SilentlyContinue
        # No first-run tips or consumer content popping up over tasks.
        New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' | Out-Null
        Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1 -Type DWord
        Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableSoftLanding 1 -Type DWord
        # Edge: no first-run wizard, sign-in or default-browser prompts over tasks.
        $edge = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
        New-Item -Force $edge | Out-Null
        foreach ($p in @{ HideFirstRunExperience = 1; BrowserSignin = 0; DefaultBrowserSettingEnabled = 0; ShowRecommendationsEnabled = 0; StartupBoostEnabled = 0 }.GetEnumerator()) {
            Set-ItemProperty $edge $p.Key $p.Value -Type DWord
        }

        # Observer: in the persona's interactive session at logon, never a service;
        # restarted daily (it swaps between requests) and after a crash or watchdog fault (UIA client leaks).
        # uiAccess binaries only start through ShellExecute (AppInfo), not the
        # scheduler's CreateProcess (error 740), so explorer launches it.
        $action = New-ScheduledTaskAction -Execute 'C:\Windows\explorer.exe' -Argument "`"$dir\uiprobe.exe`""
        # The task's own process is explorer, which exits at once, so RestartCount never sees an observer crash.
        # A crash (including the observer's own watchdog FailFast) raises Application Error 1000, which starts
        # it again after 30 s; IgnoreNew and the delay bound a crash loop to one start per 30 s.
        $crash = Get-CimClass MSFT_TaskEventTrigger root/Microsoft/Windows/TaskScheduler | New-CimInstance -ClientOnly
        $crash.Enabled = $true
        $crash.Delay = 'PT30S'
        $crash.Subscription = '<QueryList><Query Id="0" Path="Application"><Select Path="Application">' +
            "*[System[Provider[@Name='Application Error'] and EventID=1000]] and *[EventData[Data[1]='uiprobe.exe']]" +
            '</Select></Query></QueryList>'
        $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $persona), (New-ScheduledTaskTrigger -Daily -At 4am), $crash)
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

if (-not $NoCheckpoint) { Checkpoint-VM $vm -SnapshotName $Checkpoint }
