$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) { if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] } }
$vm = $cfg.LUDIUS_VM
$admin = [pscredential]::new("$($cfg.LUDIUS_ADMIN_USER)", (ConvertTo-SecureString $cfg.LUDIUS_ADMIN_PASSWORD -AsPlainText -Force))

$deps = Join-Path $PSScriptRoot 'build\deps'
$jre = Join-Path $deps 'OpenJDK25U-jre_x64_windows_hotspot_25.0.5_8.msi'
$jreSha256 = '503f1c8ce75378b1dfe89485abdbc2cb1588600937cff87e8a63d70ca5873139'
if ((Get-FileHash $jre -Algorithm SHA256).Hash -ne $jreSha256) { throw "$jre missing or not the pinned Temurin 25.0.5+8 MSI" }

$classes = Join-Path $deps 'timesheet'
javac --release 21 -d $classes (Join-Path $root 'guest\javaapp\Timesheet.java')
if ($LASTEXITCODE) { throw 'javac failed' }
$jar = Join-Path $deps 'Timesheet.jar'
jar --create --file $jar --main-class Timesheet -C $classes .
if ($LASTEXITCODE) { throw 'jar failed' }

$s = New-PSSession -VMName $vm -Credential $admin
try {
    Copy-Item -ToSession $s $jre, $jar -Destination 'C:\Windows\Temp' -Force
    Invoke-Command $s {
        param($msi, $persona)
        $ErrorActionPreference = 'Stop'
        $jhome = [Environment]::GetEnvironmentVariable('JAVA_HOME', 'Machine')
        if (-not ($jhome -and (Test-Path "$jhome\bin\WindowsAccessBridge-64.dll"))) {
            $p = Start-Process msiexec -Wait -PassThru -ArgumentList '/i', "C:\Windows\Temp\$msi", '/qn', '/norestart',
                'ADDLOCAL=FeatureMain,FeatureEnvironment,FeatureJavaHome,FeatureOracleJavaSoft'
            if ($p.ExitCode -notin 0, 3010) { throw "JRE install failed: $($p.ExitCode)" }
            $jhome = [Environment]::GetEnvironmentVariable('JAVA_HOME', 'Machine')
        }
        Remove-Item "C:\Windows\Temp\$msi"
        if (-not (Test-Path "$jhome\bin\WindowsAccessBridge-64.dll")) { throw "no Access Bridge under JAVA_HOME=$jhome" }

        $userDir = (Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -like "*\$persona" }).LocalPath
        if (-not $userDir) { throw "no profile for $persona yet: log on once first" }
        Set-Content "$userDir\.accessibility.properties" "assistive_technologies=com.sun.java.accessibility.AccessBridge`r`nscreen_magnifier_present=true" -Encoding ASCII

        $app = 'C:\Program Files\Timesheet'
        New-Item -ItemType Directory -Force $app | Out-Null
        Copy-Item -Force 'C:\Windows\Temp\Timesheet.jar' $app
        Remove-Item 'C:\Windows\Temp\Timesheet.jar'
        icacls "$app\Timesheet.jar" /reset /q | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut('C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Timesheet.lnk')
        $link.TargetPath = "$jhome\bin\javaw.exe"
        $link.Arguments = "-jar `"$app\Timesheet.jar`""
        $link.Save()

        Start-ScheduledTask -TaskName 'RangeAssist'
        "JRE at $jhome, bridge enabled for $persona, Timesheet in Start"
    } -ArgumentList (Split-Path $jre -Leaf), $cfg.LUDIUS_PERSONA_USER
}
finally { Remove-PSSession $s }
