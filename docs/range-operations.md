# Running the range (Proxmox box)

How to run a multi-day test on the Proxmox range and leave it clean afterwards. Bring-up of a fresh box is
[proxmox-bringup.md](proxmox-bringup.md); this page assumes the range is already up.

## The box

| What | Where |
| --- | --- |
| Host | `ssh root@95.216.39.232` (Proxmox) |
| Code the range runs | `/root/src/LudiusV2.new`: a plain copy, **not a git repo**. `/root/src/LudiusV2` is older; don't use it |
| Python | `/root/src/LudiusV2.new/controller/.venv` (editable install, no pytest: run the tests locally) |
| Secrets | `/root/src/LudiusV2.new/.env`. Source it, never print it |
| Run output | `/root/src/LudiusV2.new/runs` (counter, plans, days, range journals) |
| Scratch, logs, archives | `/root/warp` (snapshot logs, `warpN.out`, `warpN-runs-<ts>.tgz`) |
| Services | `ludius-agent` (systemd), Podman `ludius-temporal`, `ludius-postgres`, `ludius-vlmcsd` |

VMs (persona accounts come from `infra/ansible/group_vars/all/range-vms.yml`, VM name `ludius-<user>`):

| VMID | VM | VMID | VM |
| --- | --- | --- | --- |
| 110 | dc01 (AD, DNS, clock master) | 123 | dkoster |
| 111 | ex01 (Exchange) | 124 | pvanloon |
| 112 | chat01 (Mattermost, clock from dc01) | 125 | nokafor |
| 121 | aberg | 126 | lsmit |
| 122 | lnord | 127 | eyilmaz |
| | | 128 | sdekker |

Every remote command below starts with the same prefix:

```bash
cd /root/src/LudiusV2.new/controller && set -a && . ../.env && set +a
```

## 1. Sync the code

The box copy only changes when you push to it. Send committed `HEAD` (never your dirty tree) and keep a backup:

```bash
git archive --format=tar HEAD controller | ssh root@95.216.39.232 \
  'cd /root/src/LudiusV2.new && tar czf /root/warp/pre-sync-controller-$(date +%Y%m%d%H%M).tgz --exclude=controller/.venv controller && tar xf -'
ssh root@95.216.39.232 'cd /root/src/LudiusV2.new/controller && .venv/bin/python -c "import ludius.cli, ludius.durable, ludius.scheduled; print(\"ok\")"'
```

Comparing with `sha1sum` against a Windows checkout shows every file as changed (CRLF); compare file lists or use
`git archive` output instead.

## 2. Pre-flight

```bash
ssh root@95.216.39.232 'cd /root/src/LudiusV2.new; ls runs runs/range; cat runs/openrouter-calls.json; pgrep -af "ludius.cli"; systemctl is-active ludius-agent; podman ps --format "{{.Names}} {{.Status}}"; for v in 110 111 112 121 122 123 124 125 126 127 128; do echo -n "$v $(qm status $v | cut -d" " -f2) snaps=$(qm listsnapshot $v | grep -vc current) | "; done; echo'
```

Expect: no `ludius.cli` process, all 10 VMs running, no snapshots, agent and Temporal up.

**Clear the last run and seed the counter.** A rolled-back run still leaves its files in `runs/`, and the next run
reads them as real history. The worst offender is `runs/world/requests/`: the planner and the monitor treat
manager requests from the old run as sent, but the rollback deleted those mails. Keep only
`runs/world/narrative.yaml` (and the `*.lock` files) and `runs/portraits`; move everything else aside:

```bash
ts=$(date +%Y%m%d%H%M); A=/root/warp/pre-runs-$ts; mkdir -p $A/world
cd /root/src/LudiusV2.new && mv runs/range runs/days runs/plans runs/calls runs/monitor-* runs/*-session.jsonl $A/ 2>/dev/null
mv runs/world/requests runs/world/state.yaml runs/world/content.jsonl $A/world/ 2>/dev/null
mkdir -p $A/locks; mv runs/locks/*.day.json $A/locks/ 2>/dev/null   # day reservations of the rolled-back run
cp runs/openrouter-calls.json $A/; echo '{"decision": 0, "llm": 0}' > runs/openrouter-calls.json
ls runs runs/world   # world: narrative.yaml and locks only
```

The call caps fail closed: if `runs/openrouter-calls.json` is missing, every persona's planning fails and the warp
aborts. The `LUDIUS_CAP_DECISION`/`LUDIUS_CAP_LLM` ceilings in `.env` are totals against this file, so reset it
(or raise the caps) before each run.

## 3. Snapshot (rollback point)

RAM snapshots, dc01 and ex01 first, then the personas in parallel:

```bash
cd /root/warp
for v in 110 111 112; do qm snapshot $v pre-warp --vmstate 1 --description "warp test, rollback after" > snap-$v.log 2>&1 || echo "$v snap FAILED"; done
for v in 121 122 123 124 125 126 127 128; do (qm snapshot $v pre-warp --vmstate 1 --description "warp test, rollback after" > snap-$v.log 2>&1 || echo "$v snap FAILED") & done; wait
for v in 110 111 112 121 122 123 124 125 126 127 128; do echo -n "$v $(qm listsnapshot $v | grep -c pre-warp) | "; done; echo
```

Every VM must show `1`. Don't launch with a missing snapshot.

## 4. Launch a warp

`ludius range warp` plays N simulated weekdays on every persona with durable (Temporal) days, jumping the whole
range clock (dc01 first, skew < 60 s) forward:

- `--start YYYY-MM-DD` must be after today (forward only). Weekends are skipped. `--days` is 1–7.
- For each day: clock to the evening before at 22:00, plan (about 6 min for 8 personas), clock to just before the
  first item, play the day on the compressed clock, then the next day.
- At the end the workers stop and the clocks reset to real time. If the reset fails, the command exits 3: run
  `ludius range unwarp`. It exits 1 when any persona day had no plan or failed (see `no_plan` and `day` lines in
  `warp.jsonl`), otherwise 0. A persona whose plan has no items is skipped for that day and doesn't count as failed.
  Exit 4: a stopped warp could not confirm every day workflow closed; terminate the ids it prints.
- `LUDIUS_NO_ESCALATE=1` turns escalation off (the done contract's live-run setting).
- `--persona USER` (repeatable) limits the run; the default is everyone in `range-vms.yml`.

Detach it from the ssh session (`setsid nohup`, stdin from `/dev/null`), or it dies when ssh drops:

```bash
ssh root@95.216.39.232 'cd /root/src/LudiusV2.new/controller && set -a && . ../.env && set +a && export LUDIUS_NO_ESCALATE=1 && setsid nohup .venv/bin/python -m ludius.cli range warp --start 2026-11-02 --days 2 < /dev/null > /root/warp/warpN.out 2>&1 & sleep 20; pgrep -f "ludius.cli range warp"; tail -5 /root/warp/warpN.out' < /dev/null
```

The local ssh may still hang after the `&`; that doesn't matter, because the warp runs in its own session.

The monitor's interaction toasts need a second process: start
`.venv/bin/python -m ludius.cli range interactions` the same way, after the pre-flight (no model calls).

To stop a warp early, `kill -TERM <pid>` the `range warp` process once: it terminates the running day workflows,
stops the workers (30 s grace for all, then kill) and only then resets the clocks (`interrupted`, then
`clock_reset` in `warp.jsonl`). Give it a minute before you send another signal. Don't run `pkill -f` over ssh: the pattern also matches the
remote shell's own command line and kills it.

## 5. Watch it

```bash
ssh root@95.216.39.232 'date; pgrep -af "ludius.cli range warp"; R=/root/src/LudiusV2.new/runs; cut -c1-260 $R/range/warp.jsonl; grep -v "\"start\"" $R/range/supervisor.jsonl | tail -5; cat $R/openrouter-calls.json; tail -3 /root/warp/warpN.out'
```

- `runs/range/warp.jsonl`: the warp's own steps (clock sets, planning, day start/end, reset).
- `runs/range/supervisor.jsonl`: worker starts, exits and restarts.
- `runs/range/<user>.log`, `plan.log`: worker and planner output.
- `runs/days/<date>-<user>.jsonl`: ground truth per persona day (`item_done`, `item_abandoned`, `day_end`, ...).
- The operator monitor (interaction popups) reads the same files.

Per-day tally of item outcomes (box helper, `/root/tally.py`, not in the repo):

```bash
ssh root@95.216.39.232 'python3 /root/tally.py "/root/src/LudiusV2.new/runs/days/2026-11-0[23]-*.jsonl"'
```

## 6. Roll back and clean up

After the warp exits (check `warpN.out` for the reset):

```bash
# 1. Archive the run first: the rollback doesn't touch runs/, but the next run's pre-flight moves it.
cd /root/src/LudiusV2.new && tar czf /root/warp/warpN-runs-$(date +%Y%m%d%H%M).tgz runs

# 2. Roll back, dc01 and ex01 first
cd /root/warp; for v in 110 111 112 121 122 123 124 125 126 127 128; do qm rollback $v pre-warp > rb-$v.log 2>&1 || echo "$v rollback FAILED"; done

# 3. Clocks to real time (harmless if the warp already reset them)
cd /root/src/LudiusV2.new/controller && set -a && . ../.env && set +a && .venv/bin/python -m ludius.cli range unwarp

# 4. Restart the observer: after a RAM rollback its vsock listener is dead (errno 104) even though uiprobe runs
for v in 121 122 123 124 125 126 127 128; do qm guest exec $v --timeout 30 -- powershell -NoProfile -Command "Stop-Process -Name uiprobe -Force -EA SilentlyContinue; Start-Sleep 2; Start-ScheduledTask -TaskName RangeAssist" >/dev/null; done
sleep 20; for u in aberg lnord dkoster pvanloon nokafor lsmit eyilmaz sdekker; do echo "$u: $(timeout 40 .venv/bin/python -m ludius.cli --vm ludius-$u observe --limit 0 2>&1 | grep -E "settled=|details" | head -1 | cut -c1-120)"; done

# 5. Drop the snapshots
for v in 110 111 112 121 122 123 124 125 126 127 128; do qm delsnapshot $v pre-warp || echo "$v delsnapshot FAILED"; done
```

Each persona must print a `settled=` line in step 4. A persona that doesn't still has a dead observer; rerun step 4
for that VM.

## Gotchas

- Day workflow ids are `day-<persona>-<date>`. A warp starts a closed day again (Temporal keeps the old run's
  history) and attaches to one still running, so a rerun after a rollback plays the day anew instead of reading
  back the old result.
- A day reservation (`runs/locks/<vm>.day.json`) left by a killed or terminated day refuses the next day on that
  VM ("desktop reserved by" / "reserved day input changed"). After a rollback, move them aside (pre-flight).
- A killed warp leaves the range clocks jumped (there is no lease sweeper on Proxmox): `ludius range unwarp`.
- Start-up compensation for a guest clock set is clamped to 0–30 s; skew over 60 s between dc01 and a persona
  stops the warp.
- Clock sets check pool `ludius` membership, node and VM name first; a VM outside the pool is refused, not set.
- `ludius range up --clock realtime --today` is the realtime (scheduled, 22:00 plan / 06:30 day) mode, not a warp;
  see [proxmox-bringup.md §7](proxmox-bringup.md#7-multi-persona-run-8-personas-ludius-range).

## Scenario intranet

dc01 serves the range intranet from a folder of Markdown pages, one `<id>.md` per page (default
`controller/data/intranet`; the header format is in `controller/src/ludius/intranet.py`). For a scenario's own pages,
copy the folder, edit it, and point both sides at it: Ansible `-e intranet_dir=<dir>` and the planner's
`LUDIUS_INTRANET=<dir>` (it lists the page titles so lookups search for real pages). Check it with
`ludius intranet check <dir>`; the dc01 role runs the same check and stops before deploying a bad folder. Pages are
plain text; links may only point inside `corp.ludius.test`. Apply with `PLAYBOOK=range-exchange.yml /root/range-up.sh --tags dc01`.

Downloads: list them on a page (`files: Q4 plan.xlsx, checklist.pdf`) and put them in the folder's `files/`
(`.pdf .docx .xlsx .pptx .txt .csv .png .jpg`, 1 MB each so the guest check reads it whole, 50 MiB together). The check fails on a file that is
missing or on no page; dc01 serves them at `/files/<name>` and removes any other file there. A scenario's own pages
go in the same folder; list their titles (globs) under `pages:` in the scenario file (`LUDIUS_SCENARIO`, as
`messages:`) and noise never opens or searches for them (no click on their links, no key while one is focused or
selected), and the planner does not see them.

Personas download them with `edge_download_file` (plan params `page`, the exact page title, and `file_name`, one file
that page lists; the planner gets the list, without scenario pages, and plan load rejects any other pair). Edge saves
to the persona's `Downloads` (a noise folder, as Documents and Pictures; the observer reads it too, so this needs the
observer from the same commit). The task never starts if that file is already there; done is the file in Downloads,
byte for byte what dc01 serves. Absent at start is what proves it new: Edge sets a download's write time only when
Edge closes, so the write time is not compared.
