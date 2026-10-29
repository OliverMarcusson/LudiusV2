# infra

- `hyperv/`: the desktop Hyper-V test range (`new-vm.ps1`, `provision*.ps1`, `new-srv.ps1`, `set-clock.ps1`). In use; unchanged by the automation below.
- `packer/`: the golden Windows 10 image. On Proxmox in two stages: `base` (`proxmox-iso`: install and updates, `ansible/base.yml`) makes the template `ludius-win10-base` (8999); `golden` (`proxmox-clone` of the base: `ansible/golden.yml` and sysprep) makes `ludius-win10-golden` (9000). Hyper-V (`hyperv-iso`) runs both playbooks in one stage.
- `ansible/`: the roles (spec section 7) and the playbooks: `proxmox-host.yml` (bare Debian 13 to Proxmox VE 9), `base.yml` and `golden.yml` (applied by Packer, stage 1 and 2), `range-up.yml` (srv01 and the persona clones).
- `compose/`: Temporal and Postgres.

## Proxmox range from a bare box

The range mail server follows `mail_backend` in `group_vars/all/main.yml`: `exchange` (the default here: `range-exchange.yml`, two Windows Server VMs, below) or `grommunio` (`range-srv01.yml`, the Debian srv01 the Hyper-V test range also uses). The controller must match: `LUDIUS_MAIL_BACKEND=exchange` (see Controller settings below), unset for grommunio.

The Windows servers are installed from the ISO by Ansible, not Packer: two VMs that are never cloned, so a template buys nothing. srv01 is built by an Ansible role from the stock Debian 13 cloud image, not a Packer template. It is one VM that is never cloned, the cloud image needs no installer automation, and it is the same image and cloud-init path `hyperv/new-srv.ps1` already uses.

### Inputs

| Input | Where it goes |
| --- | --- |
| The box: Debian 13 from Hetzner `installimage`, software RAID 1, LVM VG `vg0` with a 64 GB root LV and the rest left free (the thin pool for linked clones goes there) | root SSH key access |
| Windows 10 ISO (en-GB multi-edition, Pro at index 6) and its sha256 | `scp` to `/var/lib/vz/template/iso/win10.iso`; sha256 as `iso_checksum` |
| Signed observer and the range code-signing root, from the desktop: `pwsh infra/packer/observer-artifacts.ps1` | `out/observer/uiprobe.exe` and `secrets/range-root-ca.cer` in the repo checkout on the box |
| Company narrative and personas: `runs/world/narrative.yaml` and one `controller/data/personas/<user>.yaml` per persona | the checkout on the box (`runs/` is gitignored, so copy it) |
| Persona to VM id (= vsock CID) and range address | `ansible/group_vars/all/range-vms.yml` (committed; one line per persona) |
| Exchange backend: the Windows Server 2025 evaluation ISO and the Exchange SE media (ISO, VC++ 2012/2013, URL Rewrite) | fetched by `range-exchange.yml` on the node and checked against the sha256 sums in `group_vars/all/main.yml` (WS2025 eval `26100.32230.260111-0550` en-US from the Evaluation Center, `7b0525…bfd51`; Exchange SE RTM 15.2.2562.17, KB5047155's `cd2b13…77bd1`) |
| Secrets: `ansible/group_vars/all/vault.yml` from `vault.yml.example` (per-range bring-up admin, DC admin, grommunio admin, EWS service account, user password suffix, Office MAK or KMS host) | encrypted with `ansible-vault encrypt`; its password in a file named by `ANSIBLE_VAULT_PASSWORD_FILE` |
| A build-only admin password for Packer (the account is disabled with a random password at the end of the build) | `PKR_VAR_admin_password` |

Exchange: every narrative person gets a log-in mailbox, no cap. grommunio: at most 5 personas fit its community licence (log-in mailboxes); colleagues get shared mailboxes. `range-up.yml` refuses more unless `grommunio_login_mailboxes` is raised after a licence is installed.

### Commands

On the box, as root, from a checkout of this repo (Ansible in a venv: `python3 -m venv ~/ansible && ~/ansible/bin/pip install ansible-core pywinrm`; Packer from HashiCorp's release zip):

```sh
cd infra/ansible
ansible-galaxy collection install -r requirements.yml -p collections
ansible-playbook -i localhost, -c local proxmox-host.yml     # ~30 min; stops when a reboot is needed: systemctl reboot, then run it again
# (or from another Linux/WSL machine over SSH: ansible-playbook -i <box ip>, -u root proxmox-host.yml, which reboots and continues)
cd ../..
export ANSIBLE_VAULT_PASSWORD_FILE=~/.ludius-vault-pass PKR_VAR_admin_password=...
packer init infra/packer
P="-on-error=abort -var proxmox_node=$(hostname) -var proxmox_token=$(cat /root/.ludius-packer-token) -var iso_checksum=<sha256>"
packer build -only=base.proxmox-iso.base $P infra/packer       # stage 1, ~40 min: install ~8 min, updates ~30 min
packer build -only=golden.proxmox-clone.golden $P infra/packer # stage 2: settings, Office, observer, sysprep
# start the host agent with Proxmox:Pool=ludius (docs/proxmox-bringup.md): range-up.yml checks each observer through it
cd infra/ansible && ansible-playbook range-up.yml           # ~40 min for srv01 and 5 clones in parallel
```

About 4 to 5 hours end to end, almost all of it unattended (Windows updates and the Office download dominate). Every step is re-runnable.

Measured on the rented box: Windows install to WinRM ~8 min, updates ~30 min (one reboot). Windows 10 22H2 is past end of support, so its last cumulative update (2025-10) is fixed: the base is rebuilt only for a new ISO or update policy, and a change to apps, the observer or settings is a stage 2 build. `-on-error=abort` leaves a failed build's VM running: iterate with `ansible-playbook` against it (`--start-at-task`), then destroy it (`qm destroy <id> --purge`, after checking its name) before the next build.

### What it builds

- Bridges: `vmbr1` (range, `10.77.0.0/24`, no ports, forwarding dropped; the host is `.1`), `vmbr2` (NAT build bridge `10.78.0.0/24` with DHCP, for the image build and srv01's apt).
- Template 8999 `ludius-win10-base`: Windows 10 Pro installed with virtio drivers and the QEMU guest agent, updates baked in; not generalized, keeps the build admin; only the clone source of 9000, never of a range VM.
- Template 9000: Windows 10 Pro, updates baked in (from 8999), Windows Update, Defender's scheduled scans and automatic maintenance off, Office 2024, Temurin with the Access Bridge, virtio-win including viosock, the signed observer, sysprepped.
- Pool `ludius`: the template, the range servers and every clone; an existing VM id outside it, or under another name, is refused.
- srv01 (VM 110, `10.77.0.11`, hostname `srv01`): range NIC only; an uplink NIC on `vmbr2` is attached only while it is provisioned and detached on the host afterwards whatever happened (srv01 is stopped if the detach cannot be confirmed), then the guest is checked for routes off the range. Samba AD `corp.ludius.test`, chrony as the range time source, grommunio with an outbound SMTP drop, one AD user and mailbox per narrative person.
- Exchange backend, both range NIC only, both Windows Server 2025 Standard Evaluation (180 days) from the ISO, unattended (SeaBIOS, one-off answer ISO deleted after the guest agent set the vault password):
  - dc01 (VM 110, `10.77.0.11`): AD forest `corp.ludius.test`, DNS on the range address only with no forwarders, the range time source on its own clock (w32time `NoSync`, announced reliable; members follow the domain), the range password policy (no complexity, 6+), one AD user per narrative person and the EWS service account `ludiussvc`, `mail`/`autodiscover` at ex01.
  - ex01 (VM 111, `10.77.0.12`, 16 GB): domain member, Exchange SE Mailbox role unattended with no product key (trial edition: Standard features, 180 days, then EAC reminders), a log-in mailbox per narrative person, a self-signed certificate for mail/autodiscover/ex01 and the address (fetched to `ansible/build/range-ca.crt`: the clones import it, the controller verifies EWS with it), internal URLs and the Autodiscover SCP on the mail names, EWS Basic auth over TLS, ApplicationImpersonation for `ludiussvc`. Mail domain `ludius.internal` (`mail_domain`; Exchange refuses recipients in RFC 2606 TLDs such as `.test`, so AD stays `corp.ludius.test` and only SMTP addresses move): every mailbox's primary address is `<user>@ludius.internal`. Range boundary: no send connector, `ludius.internal` the only accepted domain, a transport rule rejecting every recipient outside the organisation, all asserted, and a probe mail to `probe@example.com` through the pickup folder that must FAIL in message tracking and never be sent.
  - Two VMs rather than one: Microsoft does not recommend Exchange on a DC and a DC role change after Exchange is unsupported; 4 + 16 GB beside 8 personas at 8 GB is 84 of the box's 128 GB.
  - Each gets an uplink NIC on `vmbr2` only for its online evaluation activation (required within 10 days, else hourly shutdowns), detached host-side whatever happened (stopped if that can't be confirmed), then checked in the guest for any route off the range. The Exchange media never pass through a guest uplink: they are fetched on the node, the ISO goes in as a CD, the installers over WinRM. At the end WinRM is off on both and ex01's local Administrator disabled, through the guest agent; a server that can't be closed is stopped.
  - Personas: DNS, domain and default gateway from dc01 (it does not forward: Outlook refuses Exchange on an adapter without a gateway); Outlook builds its profile from Autodiscover at first start (`ZeroConfigExchange` in the persona hive).
- Persona VMs (ids and addresses from `range-vms.yml`): linked clones; on every run, before any start, one NIC on `vmbr1`, a `vhost-vsock-pci` device (CID = VM id) and a fixed 1920x1080 display, asserted from the running config. Joined as `WS-<USER>01`, autologon as `CORP\<user>`, persona hive settings, inactivity lock, the observer started with `--transport vsock` and checked through the host agent (`ludius observe`). Then, whether bring-up succeeded or not, the per-range bring-up admin is disabled and WinRM stopped on every bootstrapped clone through the guest agent; a clone that cannot be closed is stopped.

### Controller settings (Exchange backend)

In the repo's `.env` on the node (gitignored):

```sh
LUDIUS_MAIL_BACKEND=exchange
LUDIUS_EWS_URL=https://10.77.0.12/EWS/Exchange.asmx      # by address: the node has no range DNS; the certificate covers it
LUDIUS_EWS_USER=ludiussvc@corp.ludius.test
LUDIUS_EWS_PASSWORD_FILE=/root/.ludius-ews               # written by range-exchange.yml from the vault, 0600
LUDIUS_EWS_CA=<checkout>/infra/ansible/build/range-ca.crt
```
