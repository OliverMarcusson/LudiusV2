param([switch]$NoUiAccess, [switch]$NoCheckpoint, [string]$Checkpoint = 'clean', [switch]$RolesOnly, [string]$VM,
    [int]$WaitMinutes = 0)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = if ($VM) { $VM } else { $cfg.LUDIUS_VM }
if ($vm -notmatch '^ludius-[A-Za-z0-9-]+$') { throw "not a range VM: $vm" }
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))
$installDir = 'C:\Program Files\RangeAssist'

$roles = {
    param($persona, $personaPassword)
    $ErrorActionPreference = 'Stop'
    powercfg /change standby-timeout-ac 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
    Set-Service wuauserv -StartupType Disabled; Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
    New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1 -Type DWord
    foreach ($l in @{ Security = 1GB; System = 256MB; Application = 256MB }.GetEnumerator()) {
        wevtutil sl $l.Key /ms:$($l.Value); if ($LASTEXITCODE) { throw "wevtutil $($l.Key): $LASTEXITCODE" }
    }
    $sid = ([Security.Principal.NTAccount]$persona).Translate([Security.Principal.SecurityIdentifier]).Value
    $loaded = Test-Path "Registry::HKEY_USERS\$sid"
    $hive = if ($loaded) { "Registry::HKEY_USERS\$sid" } else { 'Registry::HKEY_USERS\LudiusPersona' }
    if (-not $loaded) { reg load 'HKU\LudiusPersona' "C:\Users\$persona\NTUSER.DAT" | Out-Null; if ($LASTEXITCODE) { throw 'reg load failed' } }
    try {
        $adv = "$hive\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
        $push = "$hive\Software\Microsoft\Windows\CurrentVersion\PushNotifications"
        $cues = "$hive\Control Panel\Accessibility\Keyboard Preference"
        foreach ($k in "$hive\Control Panel\Desktop\WindowMetrics", $adv, $push, $cues) { if (-not (Test-Path $k)) { New-Item $k | Out-Null } }
        Set-ItemProperty "$hive\Control Panel\Desktop\WindowMetrics" MinAnimate '0'
        Set-ItemProperty $adv TaskbarAnimations 0 -Type DWord
        Set-ItemProperty "$hive\Control Panel\Desktop" LogPixels 96 -Type DWord
        Set-ItemProperty "$hive\Control Panel\Desktop" Win8DpiScaling 1 -Type DWord
        Set-ItemProperty $push ToastEnabled 0 -Type DWord
        Set-ItemProperty $cues On '1'
    } finally {
        if (-not $loaded) { [gc]::Collect(); reg unload 'HKU\LudiusPersona' | Out-Null }
    }
    $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $wl AutoAdminLogon '1'; Set-ItemProperty $wl DefaultUserName $persona; Set-ItemProperty $wl DefaultPassword $personaPassword
    Remove-ItemProperty $wl AutoLogonCount -ErrorAction SilentlyContinue
    New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1 -Type DWord
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableSoftLanding 1 -Type DWord
    $edge = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
    New-Item -Force $edge | Out-Null
    foreach ($p in @{ HideFirstRunExperience = 1; BrowserSignin = 0; DefaultBrowserSettingEnabled = 0; ShowRecommendationsEnabled = 0; StartupBoostEnabled = 0 }.GetEnumerator()) {
        Set-ItemProperty $edge $p.Key $p.Value -Type DWord
    }
}

if ($RolesOnly) {
    $conflict = @('Checkpoint', 'NoUiAccess') | Where-Object { $PSBoundParameters.ContainsKey($_) }
    if ($conflict) { throw "-RolesOnly never checkpoints or builds; drop -$($conflict -join ', -')" }
    if ((Get-VM -Name $vm).State -ne 'Running') { throw "$vm is not running; -RolesOnly never starts or restores it" }
    $readback = {
        param($persona)
        $sid = ([Security.Principal.NTAccount]$persona).Translate([Security.Principal.SecurityIdentifier]).Value
        $h = "Registry::HKEY_USERS\$sid"
        function V($path, $name) { (Get-ItemProperty $path -Name $name -ErrorAction SilentlyContinue).$name }
        foreach ($l in 'Security', 'System', 'Application') { "eventlog $l max $((Get-WinEvent -ListLog $l).MaximumSizeInBytes / 1MB) MB" }
        "persona hive loaded: $(Test-Path $h)"
        "MinAnimate=$(V "$h\Control Panel\Desktop\WindowMetrics" MinAnimate)"
        "TaskbarAnimations=$(V "$h\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced" TaskbarAnimations)"
        "LogPixels=$(V "$h\Control Panel\Desktop" LogPixels) Win8DpiScaling=$(V "$h\Control Panel\Desktop" Win8DpiScaling)"
        "ToastEnabled=$(V "$h\Software\Microsoft\Windows\CurrentVersion\PushNotifications" ToastEnabled)"
        "KeyboardCues On=$(V "$h\Control Panel\Accessibility\Keyboard Preference" On)"
        "wuauserv=$((Get-Service wuauserv).StartType) NoAutoUpdate=$(V 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate)"
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        "AutoAdminLogon=$(V $wl AutoAdminLogon) DefaultUserName=$(V $wl DefaultUserName)"
    }
    $s = New-PSSession -VMName $vm -Credential $admin
    try {
        Invoke-Command $s $roles -ArgumentList $cfg.LUDIUS_PERSONA_USER, $cfg.LUDIUS_PERSONA_PASSWORD
        Invoke-Command $s $readback -ArgumentList $cfg.LUDIUS_PERSONA_USER
    }
    finally { Remove-PSSession $s }
    return
}

if (-not $NoCheckpoint -and (Get-VMSnapshot $vm -Name $Checkpoint -ErrorAction SilentlyContinue)) {
    throw "checkpoint '$Checkpoint' exists and is never replaced: pass -NoCheckpoint or -Checkpoint <new name>"
}

$until = (Get-Date).AddMinutes($WaitMinutes)
while (-not (Invoke-Command -VMName $vm -Credential $admin -ErrorAction SilentlyContinue {
    param($p) Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue | Where-Object UserName -like "*\$p" } -ArgumentList $cfg.LUDIUS_PERSONA_USER)) {
    if ((Get-Date) -gt $until) { throw "$vm not ready: no admin session or persona not logged on" }
    Start-Sleep 30
}
Get-VMDvdDrive $vm | Where-Object Path | Set-VMDvdDrive -Path $null

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
$features = if ($uiAccess) { @('--features', 'uiaccess') } else { @() }
cargo build --release -p ludius-observer @features --manifest-path (Join-Path $root 'Cargo.toml')
if ($LASTEXITCODE) { throw 'observer build failed' }
New-Item -ItemType Directory -Force $out | Out-Null
$exe = Join-Path $out 'uiprobe.exe'
Copy-Item (Join-Path $root 'target\release\uiprobe.exe') $exe -Force
if ((Set-AuthenticodeSignature $exe -Certificate $signer -HashAlgorithm SHA256).Status -notin 'Valid', 'UnknownError') { throw 'signing failed' }

$s = New-PSSession -VMName $vm -Credential $admin
try {
    Invoke-Command $s { param($dir) Get-Process uiprobe -ErrorAction SilentlyContinue | Stop-Process -Force; New-Item -ItemType Directory -Force $dir | Out-Null } -ArgumentList $installDir
    Copy-Item -ToSession $s $exe, $caFile -Destination $installDir -Force
    Invoke-Command $s {
        param($dir)
        $ErrorActionPreference = 'Stop'
        certutil -f -addstore Root "$dir\range-root-ca.cer" | Out-Null
        if ($LASTEXITCODE) { throw "certutil failed: $LASTEXITCODE" }
        Remove-Item "$dir\range-root-ca.cer"
    } -ArgumentList $installDir
    Invoke-Command $s $roles -ArgumentList $cfg.LUDIUS_PERSONA_USER, $cfg.LUDIUS_PERSONA_PASSWORD
    Invoke-Command $s {
        param($dir, $persona)
        $ErrorActionPreference = 'Stop'
        $action = New-ScheduledTaskAction -Execute 'C:\Windows\explorer.exe' -Argument "`"$dir\uiprobe.exe`""
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
    } -ArgumentList $installDir, $cfg.LUDIUS_PERSONA_USER
}
finally { Remove-PSSession $s }

if (-not $NoCheckpoint) { Checkpoint-VM $vm -SnapshotName $Checkpoint }
