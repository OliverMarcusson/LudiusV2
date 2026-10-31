# Ludius – System Specification

Oct 26, 2026 · @Oliver

## 1. Purpose and principles

Ludius generates realistic, coherent background user activity (noise) on Windows VMs in DFIR training ranges, so trainees must sift through plausible artifacts to find the scenario's real ones. It runs with no human in the loop, on simulated AD environments, in production on Proxmox and in testing on Hyper-V.

**In scope:** noise generation by personas across native, webview and web apps; range aging; a non-realtime test mode; ground-truth logging.

**Out of scope:** the scenario's attack steps (injected separately, deterministically), and any deterministic path the scenario depends on.

**Design principles**

- **Human interface only for actions.** All input goes through the hypervisor's virtual keyboard and mouse, so artifacts match a person at the console.
- **Privileged, minimal reading.** An in-guest observer reads the UI Automation tree; its forensic footprint is a design requirement.
- **Each layer has one owner.** The LLM plans and writes content, code owns structure, policy and verification, Decisions makes step-level decisions. Decisions never plans, never writes text and never sees pixels.
- **No LLM in the step loop.** LLM calls happen per day and per task, not per action.
- **Noise must be coherent, varied and non-destructive.** Full human-like tasks, no trivially filterable patterns, never damage to scenario artifacts.
- **Hypervisor-agnostic.** Everything hypervisor-specific sits behind the host agent.

## 2. Architecture

Four layers with one owner each. Code in the guest observes the screen and acts through the hypervisor; the host agent hides the hypervisor; the controller runs the step loop and calls Decisions for one action at a time; the planner decides what each persona does. Decisions never writes text or sees pixels; in planning it only classifies and selects among options code built.

&#91;embedded content: architecture · guest, host, decision and planning layers\]

The guest observer and host agent are C#; the controller and planner are Python. The observer reads but never acts: all input flows back through the host agent's virtual keyboard and mouse, so artifacts match a human at the console.

## 3. Guest observer (Windows)

The observer is a single long-running, read-only process in each persona's interactive session. It turns the screen into an element table and reports when the screen has settled. It never acts.

### Process and privileges

- Runs in the user's interactive session, started once at logon by a scheduled task. Never a service (session 0 cannot see the desktop).
- Signed with the range's own CA (root distributed via GPO) and manifested `uiAccess=true`, so it can read elevated windows without running elevated. Fallback: run as the persona at medium integrity, treating elevated windows as out of reach.
- UAC policy is set per scenario in the range GPO. A UAC prompt the persona cannot satisfy is reported as `blocked: elevation required`.
- Uses the COM UIA3 API (`IUIAutomation`) and Per-Monitor-V2 DPI awareness, so bounds are physical pixels.

### What one snapshot covers

1. The foreground window and its owned windows (modal dialogs).
2. Popups overlapping it: context menus (`#32768`), dropdowns, flyouts, tooltips.
3. The shell: taskbar (`Shell_TrayWnd`), tray overflow, Start/search when open, toast notifications.

### Snapshot method

- One `FindAllBuildCache` per window with a `CacheRequest` for all needed properties and patterns, filtered on `IsControlElement` and `IsOffscreen = false`.
- `ConnectionTimeout` and `TransactionTimeout` set, so a hung app becomes an `unresponsive` window instead of a frozen observer.
- UIA events (focus, structure, window opened/closed, menu opened, name/value changes) only trigger snapshots and settle detection. The table is always rebuilt, never patched from events.

### Element schema

| Field | UIA source |
| --- | --- |
| role | `ControlType`, `LocalizedControlType` fallback |
| name | `Name` → `LabeledBy.Name` → `LegacyIAccessible.Name` → preceding text sibling → `HelpText` |
| value | `Value`, `ToggleState`, `IsSelected`, `RangeValue`, `ExpandCollapseState` |
| state | enabled, focused, read-only, password, expanded, selected |
| bounds | `BoundingRectangle`, clipped to window and viewport |
| actions | available patterns: Invoke, Value, Toggle, SelectionItem, ExpandCollapse, Scroll |
| identity | `AutomationId`, `ClassName`, `FrameworkId`, ancestor path |
| hint | derived by code (below) |

Password fields never expose their value, only `empty` or `filled`.

**Hints** give Decisions the lookahead it cannot compute: `ExpandCollapse` → "opens list/menu"; hyperlink → "link to \<url>"; dialog buttons → "closes dialog" or "submits"; names from a destructive word list (Delete, Remove, Discard, Send, Pay, Uninstall, Format) → `destructive`; default button → "Enter triggers this"; scrollable list → "list continues, N visible".

### Framework handling

- **Chromium, Edge, Electron, WebView2** (`FrameworkId = Chrome`): the tree builds lazily once a UIA client queries; re-snapshot after \~200 ms if a `Document` is near-empty. Clip to the document viewport, since off-screen nodes are exposed. Do not use `--force-renderer-accessibility` (visible in process command lines).
- **WPF, WinUI/XAML, Explorer:** virtualized lists; report "items 1–14 shown, list continues" and let Decisions scroll.
- **Legacy Win32:** fall back to `LegacyIAccessiblePattern`.
- **Java:** Java Access Bridge enabled in the image.
- **No usable tree** (canvas apps, custom-drawn controls): send a window screenshot to the host for the visual fallback (OCR + detector), scoped to the window's bounds.

### Stable IDs, ranking and cap

- ID = hash of `ControlType + AutomationId + Name + ancestor path`, sibling index only when needed. Never `RuntimeId`. A mapping between snapshots keeps IDs stable.
- Cap at \~200 elements (Decisions' Choice allows 255 options; keys need room). Priority: focused element and container → open modal or popup (then almost nothing else) → actionable elements in reading order → shell.
- Non-actionable text goes into the state as context lines, not options.

### Settle detection

A snapshot is sent when no UIA structure/property events have arrived from the foreground process and DXGI Desktop Duplication reports no dirty rectangles, both for \~300 ms. Hard timeout \~5 s, then flagged `unsettled`. Busy cursor (`GetCursorInfo`) and indeterminate progress bars are reported.

### Forensic footprint rules

- One process for the session's lifetime; no child processes, no PowerShell, no cmd.
- Writes nothing to disk in the guest; logs and screenshots go to the host.
- No network traffic: transport is vsock (Proxmox) or Hyper-V sockets (Hyper-V).
- Installed in the golden image before any scenario activity, under a neutral name, and listed in the instructors' known-infrastructure list.
- Restarted daily between tasks and by watchdog, to contain UIA client memory leaks.
- Per-VM **pixels-only mode** (no observer, host-side visual pipeline) for hosts where any guest component is unacceptable. The element table format is identical.

## 4. Host agent and hypervisor abstraction

One host agent runs per Proxmox node and per Hyper-V host. It owns input injection, the guest channel and VM lifecycle, and exposes the same gRPC API on both hypervisors. The controller never knows which hypervisor it is talking to.

### Interfaces

- **Input:** `move(x, y)`, `click(button)`, `key_down/key_up(scancode)`, `type(text, timing)`, `ctrl_alt_del()`. Coordinates are guest physical pixels; each backend converts to its device range.
- **Channel:** a bidirectional stream per VM, relaying observer messages.
- **Lifecycle:** clone, start, stop, snapshot, rollback, delete.

### Backends

|  | Proxmox (prod) | Hyper-V (test) |
| --- | --- | --- |
| Pointer | USB tablet (`tablet: 1`), absolute 0–32767 | Synthetic mouse, `Msvm_SyntheticMouse.SetAbsolutePosition`, `ClickButton` |
| Keyboard | QMP `input-send-event`; `send-key ctrl-alt-delete` | `Msvm_Keyboard.PressKey`/`ReleaseKey`, `TypeCtrlAltDel` |
| Access | VM's QMP Unix socket (`/var/run/qemu-server/<vmid>.qmp`), node-local | WMI on the Hyper-V host |
| Guest channel | vsock (viosock driver from virtio-win) | Hyper-V sockets (AF\_HYPERV) |
| Lifecycle | Proxmox API: linked clones, snapshots | PowerShell: differencing disks, checkpoints |

RDP is never used for input, since it changes the session the observer runs in. Verify the Hyper-V WMI input methods early against the target Windows Server version; they are the least-documented part of the stack.

### Coordinates and display

- Resolution is fixed in VM config, not in the guest: a fixed display on Proxmox, `Set-VMVideo -ResolutionType Single` on Hyper-V. Guest scaling is 100%.
- Mapping: guest physical pixels → device range, rechecked whenever the observer reports a resolution or scaling change.
- Clicks land inside the element's box with small jitter and a short movement path; typing uses per-persona key timing.

### Contract test

Runs in CI against both backends: move the pointer to known pixel positions and compare with the guest's `GetCursorPos`; type a known string into a test field and read it back via UIA. This catches mapping and timing drift between test and production.

## 5. Controller and decision loop

The controller executes one subgoal at a time: observe, ask Decisions, enforce policy, act, verify. Decisions only ever chooses the next action toward one concrete subgoal.

### Step loop

1. Wait for a settled snapshot from the observer.
2. Build the Decisions state and option set in code.
3. Call Decisions once with three questions.
4. Apply policy to the chosen option; block or substitute if needed.
5. Send input through the host agent.
6. Verify on the next snapshot (code check first, Decisions `subgoal_done` as backup) and record `last_result`.

### Decisions state

```json
{
  "goal": "Open a reply to the selected mail",
  "task_context": "Replying to Anna Berg about 'Q3 report draft'",
  "window": "Inbox – Outlook",
  "focused": "[5]",
  "history": ["click [12] → mail selected"],
  "last_result": "changed",
  "elements": ["[14] button 'Reply' hint=opens compose", "..."],
  "context": ["heading: Inbox", "status: 3 unread"]
}
```

The state is plain JSON built by code; field names follow TypeSafe's current API. Only id, role, name, value, state and hint reach Decisions; bounds, actions and source stay in the controller.

### Questions per step

| Question | Type | Purpose |
| --- | --- | --- |
| `action` | Choice | Next action: element actions plus keys, filtered to the task's app |
| `subgoal_done` | Noul | Backup completion check |
| `off_track` | Noul | Screen unrelated to the subgoal → recovery step |

### Option generation

- Each option combines action and target: `click:[14]`, `key:Ctrl+R`, `type:content.body→[22]`, `scroll:down`, `rescan`.
- Only options that make sense: no typing into buttons, no apps outside the task's allowlist, no app launches unless the subgoal calls for one.
- Text options reference content prepared by the planner or text visible on screen (`type:copy[31]→[22]`). Decisions never writes text.
- Keyboard actions are always offered (Tab, Enter, Esc, arrows, common shortcuts), which tolerates perception gaps.

### Choosing

- Take the top option. Optional small variation: sample only among options close to the top probability that a code check confirms are equivalent (two Reply buttons, ribbon vs. context menu).
- Variety comes from the planning layer (section 6), not from step-level randomness.

### Policy (code-enforced, never Decisions)

- Destructive options are only offered when the task allowlists them.
- Never: clear browser history or cache, empty the Recycle Bin, run Disk Cleanup, delete or overwrite files outside the persona's noise folders, uninstall software, clear or resize event logs.
- Scenario-reserved paths, mailboxes and files are off-limits unless the scenario marks overlap as an intended red herring.
- Password fields are never typed into by noise tasks.
- Step and time budgets per task.

### Failure ladder

1. Rescan: wait for settle, re-snapshot, visual fallback.
2. In-app recovery: restricted Choice of Esc, close dialog, back.
3. Abandon the task like a human would (discard draft, close without saving).
4. Restart the app.
5. Normalize the session (close non-allowlisted apps, dismiss dialogs).
6. Sign out and back in.
7. Reboot the VM.
8. Mark the VM unhealthy, remove it from the pool, record the trace.

Noise failures normally stop at step 3: abandonment is realistic noise, while reboots leave artifacts that look like events. Every step from 4 onward is written to the ground-truth log.

### Logon and unlock

The range keeps a realistic inactivity lock. The observer reports lock state via WTS session notifications. The controller unlocks through the virtual keyboard (Ctrl+Alt+Del, password from the vault), blind, and verifies by the observer reconnecting on the user desktop. This produces real type 7 unlock logons.

## 6. Planning layer

The planner decides what each persona does, why and with what content, so noise is coherent (full tasks a person would do) and varied (hard to filter). An LLM is used here, per range, per day and per task, never per action.

Ownership is split three ways. **Code owns structure**: the day's slots and times, breaks, which tasks are eligible (installed apps, open threads, inbox, invites, files the persona owns), recipients, dependencies, budgets and verified completion. **Decisions owns interpretation and selection**: which eligible task fits a slot, and what an incoming message asks for (request, question, offer, small talk, news). **The LLM is a constrained writer**: mail bodies, documents, chat lines, file names and novel company events, for slots code fixed; it never assigns work, schedules or claims an outcome.

### Hierarchy and owners

| Level | Owner | Cadence | Example |
| --- | --- | --- | --- |
| Company narrative | LLM | Per range, revised weekly | Departments, projects, Q3 close, office move, IT outage |
| Day plan | Code (slots, eligibility), Decisions (task choice), LLM (content) | Nightly per persona | Mail in the morning, budget sheet, lunch, meeting prep |
| Task instance | LLM writer, within the slot code fixed | At plan time | Reply to Anna about the Q3 draft, with attachment |
| Subgoals | Template, or LLM-decomposed and validated | Per task | select mail → open reply → write body → send |
| Action | Decisions | Per step | `click:[14]` |
| Verification | Code; LLM only for open-ended tasks | Per subgoal / task end | Sent count +1 |

### Company narrative and world state

The LLM generates the company backstory once per range: org chart, roles, relationships, projects and a calendar of events. Each night, every persona's day plan is generated from the narrative plus the **actual world state**: inbox, files touched, calendar, open threads, and what really happened the day before. Topics, workload and rhythm therefore change over weeks.

### Personas

Each persona has a role, schedule, app set, activity mix, style sheet (mail length, tone, language, typos, signature, file naming), file habits (Desktop vs. folders), pacing distributions (typing, reading, breaks) and a risk profile for legitimate "suspicious-looking" work.

### Task templates

```yaml
task: reply_to_mail
app: outlook
preconditions: [mail_from_known_sender_unanswered]
variants:
  - [select_mail, open_reply, write_body, send]
  - [select_mail, open_mail_window, open_reply, write_body, attach_file, send]
optional: [attach_file]
subgoals:
  select_mail: {goal: "Select the mail from {sender} titled '{subject}'", done: "selected.subject == {subject}"}
  open_reply:  {goal: "Open a reply to the selected mail", done: "window 'RE: {subject}' exists"}
  write_body:  {goal: "Type the reply into the message body", text: "{content.body}"}
  send:        {goal: "Send the reply", done: "compose closed AND sent count +1"}
budget: {steps: 25, minutes: 6}
on_abandon: discard_draft
```

- Templates are skeletons: optional and reorderable subgoals, probabilistic branches; the planner picks the variant per instance.
- Completion checks read UIA state wherever possible.
- **Template growth:** the LLM proposes new templates from a capability catalog (installed apps, their features, the subgoal vocabulary). Proposals run in compressed test mode; only those that complete reliably are admitted.

### Task stack and interruptions

The controller keeps a task stack. Mid-task, the plan may push an interruption (check calendar, read a new mail, answer a chat), run it to completion and resume. Abandonment is explicit and human (discard, close without saving), never a crash.

### Content

The planner generates all text at plan time: mail bodies, document content, file names, search queries, chat messages, in the persona's style. A near-duplicate check rejects content too similar to earlier output, to avoid a uniform LLM voice.

### Cross-persona interaction

The planner orchestrates threads across personas and hosts: a mail from A creates a reply task on B's later plan; a shared document gets opened and edited by others; meeting invites get accepted. Mail chains, shares and calendars stay consistent across the range.

### Variety by filter dimension

| Dimension a trainee filters on | Variety source |
| --- | --- |
| Content and topics | Narrative-driven, per-persona style, near-duplicate check |
| Task mix | Long tail of rare tasks (expense form, print PDF, rename folder, search old mail) |
| Task structure | Variants, optional subgoals, interruptions |
| UI path | Code-chosen variants (menu, shortcut, context menu, drag) |
| App launch method | Start menu, search, taskbar, file double-click, mail link, Run (different parent processes, LNK, jump lists, UserAssist) |
| Mistakes | Wrong file opened and closed, misclick and undo, save to Downloads then move, abandoned drafts |
| Timing | Heavy-tailed per-persona distributions, circadian rhythm, late starts, breaks |
| File system | Per-persona locations and naming habits |
| Account and host | Many personas, social threads across hosts |

### Legitimate activity that resembles signal

Role-appropriate work that looks like tradecraft, with a narrative reason: IT admin PowerShell, RDP and password resets; developers downloading tools and running scripts; finance opening macro-enabled files from partners. Planned near the scenario in time and type, never overwriting or explaining away the real artifacts.

### LLM in the loop (rare)

- Fallback when Decisions is stuck in an open-ended task: one call to propose the next subgoal.
- End-of-task verification for tasks without code-checkable completion.

## 7. Clock modes, range lifecycle and provisioning

The scheduler plays a plan laid out on a logical timeline; a clock adapter decides how fast. Delivery ranges are always aged in realtime; testing never is.

### Clock modes

| Mode | Behavior | Use |
| --- | --- | --- |
| Realtime | Waits for each session's logical time; full human pacing | Delivery ranges |
| Compressed | Sessions back to back; pacing reduced to what UI settle needs | Operator, template and guardrail testing |
| Time-jump | Compressed, plus range-wide clock set to each session's logical time (DC first, via controllable NTP source) | Time-dependent tests: log rollover, retention, daily logon patterns |

Time-jump leaves clock-change events (4616) and is never used for delivery. Test runs are seeded (plan generation and any sampling) so failures can be reproduced.

### Range lifecycle

1. **Build:** Packer + Ansible produce golden images (observer, drivers, apps) and range GPOs.
2. **Age:** noise only, realtime, for the history length the base needs. Snapshot as an aged base.
3. **Per scenario:** start from the aged base; inject scenario steps deterministically, with a shorter realtime noise window around them.
4. **Snapshot the whole range together** (DCs, members, network).
5. **Deliver:** noise continues during the exercise for live-response scenarios, or stops for post-mortem analysis.
6. **Reset** to the snapshot per cohort.

The expensive aging is shared across scenarios. Within a run, VMs are persistent; whole-range snapshots avoid machine-account trust problems (alternatively, disable machine account password changes in the range GPO).

### Persistent-VM hygiene

- **Normalize before every task:** dismiss dialogs, close apps outside the allowlist, clear the per-task working folder, confirm unlocked desktop and responsive shell.
- **Side-effect log per task:** files written, apps opened, windows left; used for cleanup and audit.
- **Drift:** per-app canary tasks on a schedule detect UI changes after updates. Updates are baked into the golden image; Windows Update is paused in ranges.

### Golden image and provisioning

One Ansible playbook, applied by Packer (`proxmox-iso` and `hyperv-iso` builders), produces a Proxmox template and a Hyper-V VHDX. Roles:

| Role | Contents |
| --- | --- |
| base | Updates baked in, Windows Update paused, power never sleeps |
| hypervisor | virtio-win guest tools incl. viosock (Proxmox) or nothing (Hyper-V) |
| domain | Join simulated AD, persona accounts |
| uac | Per-scenario UAC policy |
| accessibility | Java Access Bridge, keyboard cues |
| ux\_noise | Animations off, 100% scaling, focus assist, per persona hive |
| eventlogs | Large Security, System, Sysmon log sizes so scenario events survive noise volume |
| observer | Install signed binary, range CA root, logon scheduled task |
| sysprep | Generalize (last, run by Packer) |

Per-user settings are applied to each persona's hive (as the persona after first logon during the build). Range GPOs (lock policy, log sizes, machine password policy, CA root) are managed by Ansible against the simulated DC.

## 8. Testing and quality

Quality is measured from the trainee's side: artifacts collected and parsed with the trainee toolchain, then checked for coverage, survival, coherence and filterability. Most iteration happens offline against recorded screens.

### Test layers

| Layer | Runs on | Checks |
| --- | --- | --- |
| Fixture replay | Laptop / CI, no VMs | Recorded observations + subgoal → expected Decisions choice; regression suite per app and template |
| Contract tests | Both hypervisors | Coordinate mapping, typing, channel round trip |
| Template validation | Compressed mode, Hyper-V | New and changed templates complete reliably before admission |
| Range test runs | Compressed or time-jump mode | Full metrics below |

The observer can dump snapshots (element tables plus screenshots) into `fixtures/`, which is the main development-speed lever.

### Range test metrics

Collect artifacts with the trainee toolchain (Velociraptor or KAPE collection → Plaso super-timeline), then measure:

- **Coverage:** expected artifact types per persona (UserAssist, jump lists, LNK, ShellBags, browser history, Office MRU, mail, Prefetch).
- **Volume:** timeline entries and events per persona-hour.
- **Survival:** with test scenario injections, every scenario artifact is still present after the noise (catches log rollover).
- **Coherence:** task completion rate per template; orphan actions (outside a completed or deliberately abandoned task) near zero; day summaries read like a workday.
- **Guardrails:** zero policy violations; failure rate per app (also catches UI drift).
- **Repetition:** action-sequence repetition across days and personas, content near-duplicate rate, launch-method distribution, timing regularity.

### Filterability test

1. Record a **human baseline**: people using lab VMs from the same image for several days.
2. Extract features per artifact and session: process tree shape, launch method, timing gaps, action n-grams, content similarity, file locations.
3. Train a simple classifier (LightGBM) and rule miner to separate noise from human.
4. If it succeeds, its most important features show where variety is missing; fix in the planning layer.

Target: no better than chance on the defined feature set. A simple trainee rule (account, process ancestry, time of day) must not remove most noise while keeping the signal.

### Ground truth

Every step is logged with host time and guest time, persona, window, element, action, result and task. Per scenario, this exports as an instructor timeline (noise and scenario steps), used to answer "is this relevant?" and to score trainee findings.

## 9. Tech stack, repository and CI

C# where performance and Windows integration matter (guest observer, host agent); strictly typed Python where iteration speed matters (controller, planner, analysis).

### Stack

| Component | Choice |
| --- | --- |
| Guest observer | C# / .NET 9, FlaUI (UIA3) or CsWin32 COM interop; single self-contained exe |
| Settle detection | DXGI Desktop Duplication via Vortice.Windows |
| Host agent | C# / .NET 9 on Linux (Proxmox) and Windows (Hyper-V) |
| Wire protocol | gRPC + protobuf, bidirectional streams |
| Controller, planner | Python 3.12+, asyncio, Pydantic, strict pyright |
| Orchestration | Temporal (Python SDK): durable persona-day workflows, timers, retries |
| State, traces | PostgreSQL; screenshots in S3-compatible storage (MinIO) |
| Visual fallback | Host-side ONNX Runtime detector + PaddleOCR |
| Analysis | Plaso → DuckDB + Polars; LightGBM |
| Image build | Packer + Ansible |

Check early whether FlaUI works under NativeAOT. If not, use CsWin32 with source-generated `ComWrappers`, or a self-contained single-file build.

### Repository layout

```
proto/            gRPC schemas (observation, actions, agent control)
crates/ludius-observer/  Rust – UIA snapshot, settle, vsock/hvsocket server (replaces the C# guest/observer/)
host/agent/       C# – QMP and Hyper-V backends, channel relay, lifecycle
controller/       Python – step loop, options, policy, failure ladder, clock adapters
planner/          Python – narrative, day plans, content, template generation
analysis/         Python – Plaso ingest, coverage, survival, filterability
infra/            Packer, Ansible, Temporal + Postgres deployment
fixtures/         recorded observations for offline tests
```

### CI

- Python: uv (installs, lockfile), ruff (lint, format), pyright strict, pytest including fixture replay. No compile step.
- C#: build and test on every commit; NativeAOT publish only for release builds.
- Contract tests against both hypervisors on merges to main; range test runs nightly in compressed mode.

### Open questions and risks

- [ ] Hyper-V synthetic mouse/keyboard WMI methods: verify on the target Windows Server version.
- [ ] FlaUI under NativeAOT, or the fallback interop path.
- [ ] viosock (vsock) driver behavior on the Windows guests used in ranges.
- [ ] Human baseline: who records it, how many days, which apps.
- [ ] Visual fallback quality on dense or dark-themed UIs.
- [ ] Inactivity-lock frequency in range GPOs versus unlock fragility.
- [ ] Windows licensing for the VM pool on Proxmox.
