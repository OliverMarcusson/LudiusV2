# Proxmox bring-up checklist (day 1 of the rental)

What can only be verified on the box. Everything else (QMP command shapes, Proxmox API calls, key maps, guest
channel framing, the observer's file read) is covered offline by `dotnet test host/agent.tests`, on Windows and in
`mcr.microsoft.com/dotnet/sdk:10.0` (Linux). Tick each item with the evidence (command output) next to it.

## 1. Host

- [ ] Proxmox VE 8.x installed; `pveversion -v` and `qemu-system-x86_64 --version` (QEMU >= 7.1: `screendump` needs
      `format: png`).
- [ ] `modprobe vhost_vsock && ls -l /dev/vhost-vsock` (vsock for the guest channel).
- [ ] (Installed by `infra/box/install.sh`: unit `ludius-agent`, `/opt/ludius-agent`, loopback 7451/7452, Pool `ludius`; see `infra/box/`.)
- [ ] Agent published and running as root on the node (pvesh and `/var/run/qemu-server/*.qmp` are root-only):
      `dotnet publish host/agent -f net10.0 -r linux-x64 --self-contained -c Release -o /opt/ludius-agent`.
      Config: `Agent:Backend` defaults to `proxmox` on Linux; `Proxmox:Node` (default: hostname),
      `Proxmox:Resolution` (default `1920x1080`, must match the VM display), `Proxmox:VsockPort` (default 4555, from
      `Agent:ObserverService`). Kestrel endpoints: bind where the controller can reach them, not wider.
- [ ] Range pool: `pvesh create /pools --poolid ludius`, and `Proxmox:Pool=ludius` in the agent config. Without it
      the agent refuses every VM. Only VMs in that pool on this node are touched (input, observation, lifecycle,
      clone sources); clones are created into it. Check: `ludius vm state` on a VM outside the pool is refused
      (PERMISSION_DENIED) and nothing in `/var/log/pve/tasks` names it.
- [ ] `ss -ltnp` shows only the gRPC and monitor ports; `/run/ludius-agent` is created 0700 (screendump scratch).
- [ ] Monitor at `https://monitor.ludius.se`: Tailscale on the box, `tailscale serve` to 127.0.0.1:7452, homelab Caddy
      with Claustra login. Follow `infra/monitor/README.md` (bring-up, homelab steps, verification, teardown). Agent
      environment: `Monitor__AllowedOrigins=https://monitor.ludius.se` and `Monitor__Root`. Check: `infra/monitor/check.sh`
      passes, and the monitor streams after the Claustra login.

## 2. VM config (template)

- [ ] Template in the range pool (Packer `pool`, or `pvesh set /pools/ludius --vms <id>`).
- [ ] `tablet: 1` (USB tablet, absolute pointer), PS/2 keyboard (default), guest keyboard layout en-US.
- [ ] `args: -device vhost-vsock-pci,guest-cid=<vmid>` on the template; the agent rewrites the CID on every clone.
- [ ] Fixed display: `range-up.yml` sets `vga: std` and `-global VGA.xres=1920 -global VGA.yres=1080` in every
      clone's `args` (the template's args can't be set by Packer's API token). Check `qm config <vmid>` and, in the
      guest, that 1920x1080 is the active mode after a cold boot. If the guest boots at another mode (OVMF picks the
      GOP mode, and Windows' basic display driver keeps it), set 1920x1080 in the template's OVMF setup (Device
      Manager > OVMF Platform Configuration, stored in its EFI disk) and recheck on a fresh clone. Guest scaling 100%.
      Confirm with `ludius contract --read-only` (console frame == observer screen).
- [ ] Image: virtio-win guest tools **including viosock** (driver + Winsock provider). Check in the guest:
      `netsh winsock show catalog` lists the VirtIO socket provider; note its address family (expected 40).
- [ ] Observer scheduled task started with `--transport vsock` (and `--vsock-family N` if the catalog says not 40).

## 3. QMP (one running VM)

- [ ] `ludius vm state` -> `Running`; `ludius observe` works (proves vsock, below).
- [ ] Pointer while `qm status <vmid>` and the web UI keep working (the agent never holds the QMP socket).
- [ ] `ludius contract` (full): five pointer positions within 1 px, typing probe read back exactly.
- [ ] Ctrl+Alt+Del reaches the logon screen (`ludius session unlock` on a locked VM).

## 4. vsock

- [ ] Agent log: no `vsock connect ... errno` after the guest has logged on.
- [ ] Observer restart (daily restart path) and a snapshot restore: the next `ludius observe` reconnects.
- [ ] `ludius contract --read-only`: guest file read reports a missing file as absent (needs the new observer).
- [ ] Guest file read through a junction or symlink under `Documents` is unknown, not content or absent; Desktop is
      refused (noise folders only).

## 5. Clone and snapshot

- [ ] `ludius vm clone --to ludius-win10-02` from the template: linked clone, new VM id, `args` CID == new id.
- [ ] Clone starts beside the source; both observers answer (distinct CIDs).
- [ ] `ludius vm checkpoint clean` (running: with RAM), then `ludius vm restore clean`: comes back running, observer
      reconnects; a second `checkpoint clean` is refused.
- [ ] `ludius vm delete --confirm <name>` on a throwaway clone only.

## 6. Contract test, both backends

- [ ] Proxmox: `ludius contract` PASS on two VMs. Hyper-V: `ludius contract --read-only` PASS (full run when the
      owner allows input on the test VM).
- [ ] Mail checks: `LUDIUS_SRV_HOST=<srv01 address>` in `.env` (the Hyper-V Default Switch lookup does not apply).

## 7. Multi-persona run (8 personas, `ludius range`)

- [ ] VM cap: `Agent:MaxRunningVms=8` and `Proxmox:CapExempt=110:ludius-srv01,110:ludius-dc01,111:ludius-ex01`
      (set in `infra/box/ludius-agent.service`)
      in the box's agent config. The default is 1 on both backends (the owner's desktop limit). Check: a 9th persona
      start is refused with "VM cap 8"; srv01 starts beside 8 personas.
- [x] Temporal server up: on the box `infra/box/temporal/install.sh` (Podman Quadlet units for the `infra/compose`
      stack: host networking, every port on 127.0.0.1, no firewall rules, no Docker; passwords generated into
      `/etc/ludius/compose.env`, 0600; units start at boot). Check: `systemctl status ludius-temporal`,
      `temporal operator cluster health --address 127.0.0.1:7233` (admin-tools image) says SERVING, `ss -tlnp` shows
      5432 and 723x/693x on 127.0.0.1 only. The box's timezone is the personas' (`timedatectl set-timezone
      Europe/Stockholm`, done 2026-10-28): per-persona budgets reset at host midnight.
- [ ] `.env`: `LUDIUS_CAP_DECISION`/`LUDIUS_CAP_LLM` (total ceilings: current total + run budget),
      `LUDIUS_CAP_PERSONA_DECISION`/`LUDIUS_CAP_PERSONA_LLM` (per persona and day), `LUDIUS_PERSONA_PASSWORD_<USER>`
      per persona, `LUDIUS_SRV_HOST`. `ludius range up` refuses to start without them.
- [ ] `ludius range up --clock realtime --today` (first day: plans today and starts it at once; later days come from
      the 22:00 plan and 06:30 day schedules). Under systemd: `KillMode=mixed`, so SIGTERM reaches the supervisor first
      and it stops the workers in order. Check: `ludius range status`; kill one worker (`kill -9 <pid>`): it restarts
      within seconds, its day resumes (`item_retry` in its day log), the others' logs never pause.
- [ ] Check a manual schedule trigger carries `TemporalScheduledStartTime` (ScheduledDay refuses without it): the
      `--today` days must start, not fail at `load_scheduled_day`.

## Not on Proxmox yet

- Time-jump clock mode (`set-clock.ps1`, clock sweep) is Hyper-V only; the agent's clock sweep is off on Proxmox,
  and the controller refuses `--clock time-jump` unless the agent reports Hyper-V on the controller's own machine.
  Use compressed or realtime clocks for the 7-day test. Check: `ludius day --clock time-jump` fails at once with
  "time-jump refused".
- Guest files never fall back to PowerShell Direct there: an old observer (UNIMPLEMENTED) reads as unknown.
