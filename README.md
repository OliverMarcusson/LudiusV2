# Ludius V2

Background user activity (noise) for Windows VMs in DFIR training ranges. See [the system specification](Ludius%20–%20System%20Specification.md).

Progress against the spec is tracked in [`docs/progress.html`](docs/progress.html), published at https://marcusson.dev/ludius-progress. Update its `SECTIONS` list and change log whenever work lands, then republish.

Built on local Hyper-V. An LLM planner (via OpenRouter) writes a company narrative and each persona's day plan, including all content. A day runner plays the plan through the hypervisor's virtual keyboard and mouse. The guest reads the screen through UI Automation, and Jev (TypeSafe, via OpenRouter) chooses one action per step.

## Layout

| Path | What |
| --- | --- |
| `proto/` | gRPC schemas: observation (guest channel) and host agent API |
| `guest/observer/` | C# observer (`uiprobe.exe`): UIA3 snapshots, element table, hints, stable ids, cap, settle (UIA events + DXGI dirty rects), Hyper-V socket server. Read-only; writes nothing to disk |
| `host/agent/` | C# host agent: gRPC on `127.0.0.1:7451`, Hyper-V WMI keyboard/mouse, observer relay, lifecycle, console frames |
| `controller/` | Python: planner (narrative, day plans, content, near-duplicate check), day runner (clock modes, breaks, interruptions), Jev client, options, policy, completion checks, step loop, session hygiene, unlock, failure ladder 1–8, ground truth and instructor timeline, CLI |
| `controller/data/` | Templates (Notepad, Explorer, Edge), personas, hand-written task instances |
| `fixtures/` | Recorded snapshots + console frames + expected choices, used for offline replay |
| `infra/hyperv/` | Unattended Win10 test VM, provisioning, screenshot helper |

## Setup (Hyper-V)

You need to be in Hyper-V Administrators, with .NET 10 SDK, uv and pwsh 7 installed.

```powershell
infra\hyperv\new-vm.ps1        # unattended Win10 Pro from ~\Downloads\win10.iso; credentials go to .env
infra\hyperv\provision.ps1     # range CA, signed observer (uiAccess), logon task, "clean" checkpoint
```

Add `OPENROUTER_API_KEY=...` to `.env`. The host must have Hyper-V socket service `000011cb-facb-11e6-bd58-64006a7986d3` registered under `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices`.

## Run

```powershell
dotnet run --project host\agent                    # host agent
cd controller
uv run ludius contract                             # pointer mapping + typing round trip
uv run ludius observe                              # print the current element table
uv run ludius run data\instances\q3-call-notes.yaml --restore clean [--variant N] [--clock realtime]
uv run ludius observe --record NAME                # new fixture
uv run ludius session lock|unlock|normalize

uv run ludius plan narrative --date 2026-10-26     # once per range -> runs/world/narrative.yaml
uv run ludius plan day --date 2026-10-27           # per persona and day -> runs/plans/<date>-<user>.yaml
uv run ludius day ..\runs\plans\2026-10-27-aberg.yaml --restore clean [--clock realtime]
uv run ludius timeline ..\runs\days\*.jsonl --out ..\runs\timeline.csv
```

Each run writes a ground-truth JSONL file to `runs/` (gitignored); days go to `runs/days/`, and the planner reads them back as world state.

## Checks

```powershell
cd controller
uv run ruff check src tests; uv run ruff format --check src tests; uv run pyright; uv run pytest
$env:LUDIUS_LIVE_JEV=1; uv run pytest -k jev_choice    # live Jev replay on fixtures
dotnet build Ludius.sln
```

## Verified (2026-10-26, Win10 22H2 guest, 1920×1080)

- Contract: the pointer lands within 1 px. Hyper-V's absolute pointer rounds low coordinates down by one, and an absolute (0,0) is ignored. Typed ASCII, symbols included, read back exactly via UIA.
- All 4 Notepad variants (Start search or Run dialog, Ctrl+S or File menu) completed from the clean checkpoint in 9–12 Jev steps. The saved file's content is byte-exact. One Jev call costs about $0.0001.
- The observer runs with `uiAccess` and DXGI settle. A settled snapshot takes about 0.3–0.5 s.
- Explorer (4 variants) and Edge (2 variants) templates complete live. Lock/unlock, sign-out/sign-in and normalize (closing unsaved Notepads and Explorer windows) all work.
- Planner: narrative and a day plan for 2026-10-27 cost about $0.05 (Claude Sonnet 5.5). The day (8 items: 3 notes, 2 web lookups, a folder, 2 breaks, one interruption) ran 8/8 in compressed mode on the VM; the interrupted Notepad task resumed and finished. Breaks lock the screen and unlock again.

## Deviations from the spec

- .NET 10 instead of 9: only the 10 SDK is installed, and .NET 9 support ends in November 2026.
- The guest observer listens and the host agent connects. Overlapped `ConnectAsync` fails on AF_HYPERV, so the connect is a blocking call on a worker thread.
- `uiAccess` binaries can't start through Task Scheduler's CreateProcess (error 740), so the logon task launches the observer via `explorer.exe`. A new instance replaces the running one, which is how the daily restart works.
- Keys use virtual-key codes (the Hyper-V WMI API), not scancodes.
- Completion checks are structured YAML (`window`, `foreground`, `element`, `typed`), not free-form strings.

## Not built yet

Cross-persona threads, Outlook/Office templates, template growth, time-jump mode, Temporal/Postgres, Proxmox backend and vsock, visual fallback (OCR/detector), clone/delete lifecycle, non-ASCII typing, and the analysis/filterability pipeline.
