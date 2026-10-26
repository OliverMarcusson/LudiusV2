# Checks one-vm.ps1 with stubbed VMs: no Hyper-V call, nothing is started. Get-VM is replaced by a JSON state file.
#  1. Four processes race to activate four Saved VMs; each holds the window between check and "start" open for
#     0.8 s. Exactly one may start; the rest must be refused by the recheck under the lock.
#  2. Fail closed: while another process holds the lock, an activation with a 1 s lock timeout is refused.
$ErrorActionPreference = 'Stop'
$dir = Join-Path ([IO.Path]::GetTempPath()) "ludius-onevm-$PID"
New-Item -ItemType Directory -Force $dir | Out-Null
$state = Join-Path $dir 'vms.json'
$lib = Join-Path $PSScriptRoot 'one-vm.ps1'
$names = 'ludius-a', 'ludius-b', 'ludius-c', 'ludius-d'
$names | ForEach-Object { [pscustomobject]@{ Name = $_; State = 'Saved' } } | ConvertTo-Json | Set-Content $state

$actor = @'
param($lib, $state, $me, $lockSeconds)
. $lib
function Get-VM { param($Name) Get-Content $state | ConvertFrom-Json }
try {
    Invoke-OneVm $me -LockSeconds $lockSeconds {
        Start-Sleep -Milliseconds 800  # the race window a lock-free check-then-start would lose
        $vms = @(Get-Content $state | ConvertFrom-Json)
        foreach ($v in $vms) { if ($v.Name -eq $me) { $v.State = 'Running' } }
        $vms | ConvertTo-Json | Set-Content $state
    }
    "started $me"
} catch { "refused $me : $_" }
'@
$actorFile = Join-Path $dir 'actor.ps1'
Set-Content $actorFile $actor

$procs = foreach ($n in $names) {
    Start-Process pwsh -ArgumentList '-NoProfile', '-NonInteractive', '-File', $actorFile, $lib, $state, $n, 60 `
        -RedirectStandardOutput (Join-Path $dir "$n.out") -NoNewWindow -PassThru
}
$procs | Wait-Process
$out = $names | ForEach-Object { Get-Content (Join-Path $dir "$_.out") }
$out
$running = @(Get-Content $state | ConvertFrom-Json | Where-Object State -eq 'Running')
if (@($out -like 'started *').Count -ne 1 -or $running.Count -ne 1) { throw "race: $($running.Count) VMs running" }
"ok: 1 of $($names.Count) racing activations started ($($running.Name)), the rest refused"

# Fail closed: a holder keeps the lock 5 s; a 1 s activation attempt must be refused without activating.
$names | ForEach-Object { [pscustomobject]@{ Name = $_; State = 'Saved' } } | ConvertTo-Json | Set-Content $state
$holder = Start-Process pwsh -ArgumentList '-NoProfile', '-NonInteractive', '-Command',
    "`$m = [Threading.Mutex]::new(`$false, 'Global\ludius-vm-activation'); [void]`$m.WaitOne(); Start-Sleep 5; `$m.ReleaseMutex()" `
    -NoNewWindow -PassThru
Start-Sleep 1
$r = & pwsh -NoProfile -NonInteractive -File $actorFile $lib $state 'ludius-a' 1
$holder | Wait-Process
$r
if ($r -notlike 'refused ludius-a : one-VM cap: activation lock busy*' -or
    @(Get-Content $state | ConvertFrom-Json | Where-Object State -eq 'Running').Count) { throw 'lock timeout did not fail closed' }
'ok: busy lock fails closed'
Remove-Item -Recurse $dir
