# Ludius V2

Background user activity (noise) for Windows VMs in DFIR training ranges. See [the system specification](Ludius%20–%20System%20Specification.md).

Built on local Hyper-V. An LLM planner (via OpenRouter) writes a company narrative and each persona's day plan, including all content. A day runner plays the plan through the hypervisor's virtual keyboard and mouse. The guest reads the screen through UI Automation, and a Decisions model (via OpenRouter, default `liquid/d1.1`) chooses one action per step.

## Layout

| Path | What |
| --- | --- |
| `proto/` | gRPC schemas: observation (guest channel) and host agent API |
| `crates/ludius-observer/` | Rust observer (`uiprobe.exe`), replacing the C# `guest/observer/`: UIA3 snapshots, element table, hints, stable ids, cap, settle (UIA events + DXGI dirty rects), Hyper-V socket or vsock server. Read-only; writes nothing to disk |
| `host/agent/` | C# host agent: gRPC on `127.0.0.1:7451`, Hyper-V WMI keyboard/mouse, observer relay, lifecycle, console frames |
| `crates/ludius/` | Rust `ludius` binary: planner (narrative, day plans, content, near-duplicate check), day runner (clock modes, breaks, interruptions), Decisions client, options, policy, completion checks, step loop, session hygiene, unlock, failure ladder, ground truth and instructor timeline, range supervisor, CLI |
| `data/` | Templates (Notepad, Explorer, Edge), personas, hand-written task instances |
| `web/console/` | Operator monitor: view-only desktop feeds of the range VMs (React, shadcn/ui), served by the host agent |
| `fixtures/` | Recorded snapshots + console frames + expected choices, used for offline replay |
| `infra/hyperv/` | Unattended Win10 test VM, provisioning, screenshot helper |

## Setup (Hyper-V)

You need to be in Hyper-V Administrators, with .NET 10 SDK, Rust (cargo), uv (only for the ISO helpers) and pwsh 7 installed.

```powershell
infra\hyperv\new-vm.ps1        # unattended Win10 Pro from ~\Downloads\win10.iso; credentials go to .env
infra\hyperv\provision.ps1     # range CA, signed observer (uiAccess), logon task, "clean" checkpoint
```

Add `OPENROUTER_API_KEY=...` to `.env`. The host must have Hyper-V socket service `000011cb-facb-11e6-bd58-64006a7986d3` registered under `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices`.

## Run

```powershell
dotnet run --project host\agent                    # host agent
cargo build --release -p ludius --bin ludius    # -> targetelease\ludius.exe; it reads .env and data\ from the checkout
ludius observe                                     # print the current element table
ludius run data\instances\q3-call-notes.yaml --restore clean [--variant N] [--clock realtime]
ludius canary [APP...]                             # one task per app as a smoke test

ludius plan narrative --date 2026-10-26            # once per range -> runs/world/narrative.yaml
ludius plan day --persona aberg --date 2026-10-27  # per persona and day -> runs/plans/<date>-<user>.yaml
ludius day runs\plans6-10-27-aberg.yaml [--clock realtime|compressed]
ludius timeline runs\days\*.jsonl --out runs	imeline.csv
```

`ludius` with no arguments prints every command (range supervisor, warp, schedules, metrics, store).

Each run writes a ground-truth JSONL file to `runs/` (gitignored); days go to `runs/days/`, and the planner reads them back as world state.

## Operator monitor

A view-only web page with a 12 fps video feed of every range VM's desktop; a tile opens a larger view. Frames come from Hyper-V's console thumbnail on the host (nothing runs in the guest for this): about 22 ms per frame at 640 px and 100 ms at 1280 px, so the agent captures up to three frames in parallel and serves each VM and size from one shared capture loop over a WebSocket. Measured: 12 fps at 640 and 960 px, about 11.5 at 1280 px (the focus view), and three viewers of one VM at 11.8 fps each. Full 1080p frames take 350 ms, so the feed is capped at 1280 px.

```powershell
cd web\console; bun install; bun run build   # the host agent serves web/console/dist
dotnet run --project hostgent              # then open http://127.0.0.1:7452
bun run dev                                    # development, proxies /api to the agent
```

The monitor runs on its own HTTP/1.1 endpoint (`Kestrel:Endpoints:Monitor`, default `127.0.0.1:7452`) with read-only access to the range: it cannot send input or change VM state. The only write route generates console portrait assets. Only VMs whose names match `Monitor:Include` (default `^ludius-`) are listed or served. The stream rejects WebSocket connections from other origins, and the agent only answers to the host names in `AllowedHosts` (DNS rebinding); to reach it by another name, set the `AllowedHosts` environment variable on that machine (for example `127.0.0.1;localhost;myhost`) rather than editing `appsettings.json`. It has no authentication, so keep it on localhost, or bind it to an operator network only behind a reverse proxy with auth.

The console includes **Overview** (desktop feeds, offline personas, interaction popups and latest recorded day), **Company** (narrative, projects, calendar and reporting relationships), **People** (persona styles, hours and pacing), and **Daybook** (planned items beside recorded outcomes, abandonment reasons and controller checks). Date/person links are shareable. Daybook uses the latest run in an append-only day log; the full record retains earlier attempts. Verified connections come from the existing recent interaction feed, not from planned handoffs.

`Monitor:Runs` selects the controller's runs directory, defaulting to the directory containing `Monitor:Calls`. `Monitor:Personas` selects the persona YAML directory (default `../../data/personas`, relative to the agent content root). Set both to absolute paths for a published agent outside the checkout. The new GET routes `/api/console` and `/api/daybook/{YYYY-MM-DD}/{user}` read bounded files and expose selected display fields only; they make no model calls and cannot act on the range. Missing or unreadable records stay unavailable. Files over 16 MiB are refused rather than presenting partial history as complete.

Portraits are stored outside the UI bundle in `Monitor:Portraits` (default `<Monitor:Runs>/portraits`). Configure `OPENROUTER_API_KEY` in the agent environment, or `Monitor:OpenRouterKeyFile` with the absolute path of an existing .env file containing that key. The key stays server-side. `GET /api/portraits` lists saved images; `POST /api/portraits/{user}` generates a missing portrait with `qwen/qwen-image-2.1-turbo`; `?replace=true` explicitly regenerates one. Only accounts in the persona/company data are accepted, one generation runs at a time, and failed generation keeps the existing image. POST requires JSON and a trusted Origin; keep the existing authenticated reverse proxy in front of the console. Portraits never enter the guest VMs and are separate from controller request counters. The appearance toggle remembers light/dark mode in the browser.

The header also shows two OpenRouter counters, **Decision calls** (Decisions API) and **LLM calls** (chat completions). Each counts one HTTP request sent to OpenRouter, so a retry or validation re-ask that sends another request counts again. Every controller process adds to the same totals in `runs/openrouter-calls.json` (`Monitor:Calls`). The totals survive restarts and keep growing until you delete that file; the page polls them every 2 s. A missing or unreadable counter file shows unavailable.

## Checks

```powershell
$env:CARGO_INCREMENTAL=0; cargo test -p ludius
dotnet build Ludius.sln
cd web\console; bun run lint; bun run build
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

## Status

The Verified section above is a dated snapshot from 2026-10-26.

Built, with partial verification:
- Hyper-V guest, host and controller layers; visual fallback (PP-OCR and OmniParser) when a window has no UI tree; template growth with code admission; `ludius day --temporal` runs, on demand; a PostgreSQL mirror of ground truth.
- Partial: cross-persona handoffs (mistake admission pending), clone lifecycle (guarded delete tested, no real deletion), pixels-only mode, and filterability (provenance and a held-out split only).

Not built, or not verified:
- Outlook/Office and mail templates; Proxmox backend and vsock (both deferred by the owner).
- The deployed Temporal, PostgreSQL and MinIO stack is not running. The compose file is config only. The S3 content-hash test passed once against the stock MinIO binary; no official container tag is claimed.
- The pixels-only close task still fails.
- Human baseline (deferred); Plaso, DuckDB, Polars and LightGBM analysis.
- Non-ASCII typing (the planner rejects it).
