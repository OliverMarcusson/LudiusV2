# infra

- `hyperv/`: the desktop Hyper-V test range (`new-vm.ps1`, `provision*.ps1`, `new-srv.ps1`, `set-clock.ps1`). In use; unchanged by the automation below.
- `packer/`: the golden Windows 10 image, `proxmox-iso` and `hyperv-iso` from one template, provisioned by `ansible/golden.yml`.
- `ansible/`: the roles (spec section 7) and three playbooks: `proxmox-host.yml` (bare Debian 13 to Proxmox VE 9), `golden.yml` (applied by Packer), `range-up.yml` (srv01 and the persona clones).
- `compose/`: Temporal and Postgres.

## Proxmox range from a bare box

srv01 is built by an Ansible role from the stock Debian 13 cloud image, not a Packer template. It is one VM that is never cloned, the cloud image needs no installer automation, and it is the same image and cloud-init path `hyperv/new-srv.ps1` already uses.

### Inputs

| Input | Where it goes |
| --- | --- |
| The box: Debian 13 from Hetzner `installimage`, software RAID 1, LVM VG `vg0` with a 64 GB root LV and the rest left free (the thin pool for linked clones goes there) | root SSH key access |
| Windows 10 ISO (en-GB multi-edition, Pro at index 6) and its sha256 | `scp` to `/var/lib/vz/template/iso/win10.iso`; sha256 as `iso_checksum` |
| Signed observer and the range code-signing root, from the desktop: `pwsh infra/packer/observer-artifacts.ps1` | `out/observer/uiprobe.exe` and `secrets/range-root-ca.cer` in the repo checkout on the box |
| Company narrative and personas: `runs/world/narrative.yaml` and one `controller/data/personas/<user>.yaml` per persona | the checkout on the box (`runs/` is gitignored, so copy it) |
| Persona to VM id (= vsock CID) and range address | `ansible/group_vars/all/range-vms.yml` (committed; one line per persona) |
| Secrets: `ansible/group_vars/all/vault.yml` from `vault.yml.example` (per-range bring-up admin, DC admin, grommunio admin, user password suffix, Office MAK or KMS host) | encrypted with `ansible-vault encrypt`; its password in a file named by `ANSIBLE_VAULT_PASSWORD_FILE` |
| A build-only admin password for Packer (the account is disabled with a random password at the end of the build) | `PKR_VAR_admin_password` |

At most 5 personas fit grommunio's community licence (log-in mailboxes); colleagues get shared mailboxes. `range-up.yml` refuses more unless `grommunio_login_mailboxes` is raised after a licence is installed.

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
packer build -only=proxmox-iso.proxmox -var proxmox_node=$(hostname) \
  -var proxmox_token=$(cat /root/.ludius-packer-token) -var iso_checksum=<sha256> infra/packer   # ~2.5 to 4 h
# start the host agent with Proxmox:Pool=ludius (docs/proxmox-bringup.md): range-up.yml checks each observer through it
cd infra/ansible && ansible-playbook range-up.yml           # ~40 min for srv01 and 5 clones in parallel
```

About 4 to 5 hours end to end, almost all of it unattended (Windows updates and the Office download dominate). Every step is re-runnable.

### What it builds

- Bridges: `vmbr1` (range, `10.77.0.0/24`, no ports, forwarding dropped; the host is `.1`), `vmbr2` (NAT build bridge `10.78.0.0/24` with DHCP, for the image build and srv01's apt).
- Template 9000: Windows 10 Pro, updates baked in, Windows Update, Defender's scheduled scans and automatic maintenance off, Office 2024, Temurin with the Access Bridge, virtio-win including viosock, the signed observer, sysprepped.
- Pool `ludius`: the template, srv01 and every clone; an existing VM id outside it, or under another name, is refused.
- srv01 (VM 110, `10.77.0.11`, hostname `srv01`): range NIC only; an uplink NIC on `vmbr2` is attached only while it is provisioned and removed afterwards, with a check that no route leaves the range. Samba AD `corp.ludius.test`, chrony as the range time source, grommunio with an outbound SMTP drop, one AD user and mailbox per narrative person.
- Persona VMs (ids and addresses from `range-vms.yml`): linked clones; on every run, before any start, one NIC on `vmbr1`, a `vhost-vsock-pci` device (CID = VM id) and a fixed 1920x1080 display, asserted from the running config. Joined as `WS-<USER>01`, autologon as `CORP\<user>`, persona hive settings, inactivity lock, the observer started with `--transport vsock` and checked through the host agent (`ludius observe`). Then the per-range bring-up admin is disabled and WinRM stopped.
