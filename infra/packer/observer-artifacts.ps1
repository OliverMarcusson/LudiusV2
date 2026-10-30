$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$ca = Get-ChildItem Cert:\CurrentUser\My | Where-Object Subject -eq 'CN=Ludius Range Root CA' | Select-Object -First 1
$signer = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object Subject -eq 'CN=Ludius Range Code Signing' | Select-Object -First 1
if (-not ($ca -and $signer)) { throw 'no range CA or code-signing certificate in CurrentUser\My (provision.ps1 creates them)' }
$secrets = Join-Path $root 'secrets'
New-Item -ItemType Directory -Force $secrets | Out-Null
Export-Certificate -Cert $ca -FilePath (Join-Path $secrets 'range-root-ca.cer') | Out-Null
$out = Join-Path $root 'out\observer'
dotnet publish (Join-Path $root 'guest\observer') -c Release -o $out -p:UiAccess=true -nologo -v q
if ($LASTEXITCODE) { throw 'observer publish failed' }
$exe = Join-Path $out 'uiprobe.exe'
if ((Set-AuthenticodeSignature $exe -Certificate $signer -HashAlgorithm SHA256).Status -notin 'Valid', 'UnknownError') { throw 'signing failed' }
"signed $exe; root $(Join-Path $secrets 'range-root-ca.cer')"
