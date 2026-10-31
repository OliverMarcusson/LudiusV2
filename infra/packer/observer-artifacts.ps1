$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$ca = Get-ChildItem Cert:\CurrentUser\My | Where-Object Subject -eq 'CN=Ludius Range Root CA' | Select-Object -First 1
$signer = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object Subject -eq 'CN=Ludius Range Code Signing' | Select-Object -First 1
if (-not ($ca -and $signer)) { throw 'no range CA or code-signing certificate in CurrentUser\My (provision.ps1 creates them)' }
$secrets = Join-Path $root 'secrets'
New-Item -ItemType Directory -Force $secrets | Out-Null
Export-Certificate -Cert $ca -FilePath (Join-Path $secrets 'range-root-ca.cer') | Out-Null
$out = Join-Path $root 'out\observer'
cargo build --release -p ludius-observer --features uiaccess --manifest-path (Join-Path $root 'Cargo.toml')
if ($LASTEXITCODE) { throw 'observer build failed' }
New-Item -ItemType Directory -Force $out | Out-Null
$exe = Join-Path $out 'uiprobe.exe'
Copy-Item (Join-Path $root 'target\release\uiprobe.exe') $exe -Force
if ((Set-AuthenticodeSignature $exe -Certificate $signer -HashAlgorithm SHA256).Status -notin 'Valid', 'UnknownError') { throw 'signing failed' }
"signed $exe; root $(Join-Path $secrets 'range-root-ca.cer')"
