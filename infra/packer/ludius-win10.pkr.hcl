# Golden Windows 10 image (spec section 7), on Proxmox in two stages so the slow part is not redone:
#   stage 1, build "base": proxmox-iso, Windows install (~8 min) + infra/ansible/base.yml (updates, ~30 min) -> template
#     ludius-win10-base (8999), not sysprepped. Rebuilt only when the ISO or the update policy changes; Windows 10
#     22H2 is past end of support, so its last cumulative update is fixed and the base rarely needs a rebuild.
#   stage 2, build "golden": proxmox-clone of the base + infra/ansible/golden.yml (settings, apps, observer, viosock)
#     + sysprep -> template ludius-win10-golden (9000), the range's linked-clone source. Minutes, not an hour.
# Hyper-V (parity with the test host: a generalized VHDX) stays one stage, hyperv-iso + base.yml + golden.yml: there is
# no template to clone there, and splitting it would mean exporting and re-importing a VHDX for a rare build.
# Same answer file for both installs (autounattend.xml.pkrtpl, built into a CD by Packer, so no password is in a
# committed file), WinRM for Packer and Ansible during the build, sysprep last as a Packer provisioner. Persona
# accounts, domain join and the observer's logon task are per clone (range-up.yml).
#
# Run from the repo root (relative paths below), with ANSIBLE_VAULT_PASSWORD_FILE set and PKR_VAR_admin_password set
# (a build-only password, the same for both stages: stage 2 disables the build admin with a random password at the
# end; the base template keeps it, and is never cloned into the range):
#   packer init infra/packer
#   packer build -on-error=abort -only=base.proxmox-iso.base -var proxmox_token=$(cat /root/.ludius-packer-token) \
#     -var iso_checksum=<sha256 of win10.iso> infra/packer                          # on the Proxmox node, ~40 min
#   packer build -on-error=abort -only=golden.proxmox-clone.golden -var proxmox_token=... -var iso_checksum=<same> \
#     infra/packer
# -on-error=abort leaves a failed build's VM running for direct ansible-playbook runs (destroy it before rebuilding).
# Hyper-V (desktop): needs oscdimg (Windows ADK) for the answer CD and an Ansible control node, which Windows itself
# can't be; the build creates and starts a VM, so only with the owner's OK and every other ludius-* VM Off or Saved:
#   . infra/hyperv/one-vm.ps1; Invoke-OneVm ludius-win10-pk { packer build -only=golden.hyperv-iso.hyperv ... infra/packer }

packer {
  required_version = ">= 1.11.0"
  required_plugins {
    proxmox = {
      source  = "github.com/hashicorp/proxmox"
      version = "= 1.2.4"
    }
    hyperv = {
      source  = "github.com/hashicorp/hyperv"
      version = "= 1.1.5"
    }
    ansible = {
      source  = "github.com/hashicorp/ansible"
      version = "= 1.1.6"
    }
  }
}

variable "admin_user" {
  type    = string
  default = "rangeadmin"
}

variable "admin_password" {
  type        = string
  sensitive   = true
  description = "Build admin password: from PKR_VAR_admin_password, never a file. Build only: the account is disabled at the end."
  validation {
    condition     = can(regex("^[A-Za-z0-9!#%()*+,./:;=?@\\[\\]^_{|}~-]{12,64}$", var.admin_password))
    error_message = "12 to 64 characters, no quotes, spaces, <, >, & or $ (it goes into XML and command lines)."
  }
}

variable "image_index" {
  type        = number
  default     = 6
  description = "Windows 10 Pro in the en-GB multi-edition ISO (infra/hyperv/autounattend.xml)."
}

variable "iso_checksum" {
  type        = string
  description = "sha256 of the Windows ISO. No default: an unchecked ISO is never built."
}

# --- Proxmox ---
variable "proxmox_url" {
  type    = string
  default = "https://127.0.0.1:8006/api2/json"
}

variable "proxmox_node" {
  type        = string
  default     = ""
  description = "Node name (hostname of the box)."
}

variable "proxmox_token_id" {
  type    = string
  default = "root@pam!packer" # made by the proxmox_host role
}

variable "proxmox_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "template_vmid" {
  type    = number
  default = 9000 # range-up.yml's template_vmid
}

variable "base_vmid" {
  type    = number
  default = 8999 # stage 1 template, the clone source of stage 2
}

variable "range_pool" {
  type    = string
  default = "ludius" # the host agent's Proxmox:Pool; range-up.yml refuses a template outside it
}

variable "storage" {
  type    = string
  default = "local-lvm"
}

variable "build_bridge" {
  type    = string
  default = "vmbr2" # NAT: Windows Update, Office CDN, Temurin download
}

# --- Hyper-V ---
variable "hyperv_iso_path" {
  type    = string
  default = "${env("USERPROFILE")}/Downloads/win10.iso" # new-vm.ps1's -Iso default; a local file, never a download
}

variable "hyperv_vm_name" {
  type    = string
  default = "ludius-win10-pk"
  validation {
    condition     = can(regex("^ludius-[A-Za-z0-9-]+$", var.hyperv_vm_name))
    error_message = "A range VM name: ludius-*, never an existing VM."
  }
}

locals {
  autounattend = templatefile("${abspath(path.root)}/autounattend.xml.pkrtpl", {
    admin_user     = var.admin_user
    admin_password = var.admin_password
    image_index    = var.image_index
  })
  # The DVD loader's "press any key", answered like new-vm.ps1 does (Enter, every 500 ms).
  any_key = ["<enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter>"]
}

# Stage 1: install and updates, kept as the template the golden image is cloned from.
source "proxmox-iso" "base" {
  proxmox_url              = var.proxmox_url
  insecure_skip_tls_verify = true # the node's own self-signed API certificate, on loopback
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token
  node                     = var.proxmox_node
  vm_id                    = var.base_vmid
  vm_name                  = "ludius-win10-base"
  template_description     = "Ludius Windows 10 base (infra/packer stage 1): installed and updated, not generalized; build only, never a range clone source"
  pool                     = var.range_pool

  boot_iso {
    type         = "ide"
    index        = 0
    iso_file     = "local:iso/win10.iso"
    iso_checksum = "sha256:${var.iso_checksum}"
    unmount      = true
  }
  additional_iso_files {
    type     = "ide"
    index    = 1
    iso_file = "local:iso/virtio-win-0.1.302.iso" # proxmox-host.yml's golden_virtio_iso_url: has viosock
    unmount  = true
  }
  additional_iso_files {
    type             = "ide"
    index            = 3
    cd_content       = { "autounattend.xml" = local.autounattend }
    cd_label         = "AUTOUNATTEND"
    iso_storage_pool = "local"
    unmount          = true
  }

  os       = "win10"
  machine  = "q35"
  bios     = "ovmf"
  cpu_type = "x86-64-v3" # portable across rented boxes (AVX2, AES); `host` would tie a restored template to one CPU model
  cores    = 4
  memory   = 6144
  efi_config {
    efi_storage_pool  = var.storage
    efi_type          = "4m"
    pre_enrolled_keys = true
  }
  scsi_controller = "virtio-scsi-single"
  disks {
    type         = "scsi"
    disk_size    = "64G"
    storage_pool = var.storage
    format       = "raw"
    discard      = true
    io_thread    = true
  }
  network_adapters {
    model  = "virtio"
    bridge = var.build_bridge
  }
  vga {
    type   = "std"
    memory = 32
  }
  qemu_agent = true

  boot_wait    = "3s"
  boot_command = local.any_key

  communicator   = "winrm"
  winrm_username = var.admin_user
  winrm_password = var.admin_password
  winrm_timeout  = "2h"
  task_timeout   = "20m"
}

# Stage 2: a full clone of the base (the golden template must not depend on the base's disk), finished and
# generalized. Hardware is restated: the clone builder would otherwise apply its own defaults over the base's.
source "proxmox-clone" "golden" {
  proxmox_url              = var.proxmox_url
  insecure_skip_tls_verify = true
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token
  node                     = var.proxmox_node
  clone_vm_id              = var.base_vmid
  full_clone               = true
  vm_id                    = var.template_vmid
  vm_name                  = "ludius-win10-golden"
  template_description     = "Ludius golden Windows 10 (infra/packer stage 2), generalized"
  pool                     = var.range_pool

  # The hypervisor role stages viosock from it.
  additional_iso_files {
    type     = "ide"
    index    = 1
    iso_file = "local:iso/virtio-win-0.1.302.iso"
    unmount  = true
  }

  os              = "win10"
  machine         = "q35"
  bios            = "ovmf"
  cpu_type        = "x86-64-v3"
  cores           = 4
  memory          = 6144
  scsi_controller = "virtio-scsi-single"
  network_adapters {
    model  = "virtio"
    bridge = var.build_bridge
  }
  vga {
    type   = "std"
    memory = 32
  }
  qemu_agent = true

  communicator   = "winrm"
  winrm_username = var.admin_user
  winrm_password = var.admin_password
  winrm_timeout  = "30m"
  task_timeout   = "20m"
}

source "hyperv-iso" "hyperv" {
  vm_name      = var.hyperv_vm_name
  iso_url      = var.hyperv_iso_path
  iso_checksum = "sha256:${var.iso_checksum}"
  cd_content   = { "autounattend.xml" = local.autounattend }
  cd_label     = "AUTOUNATTEND"

  generation            = 2
  enable_secure_boot    = true
  secure_boot_template  = "MicrosoftWindows"
  cpus                  = 4
  memory                = 6144
  enable_dynamic_memory = false
  disk_size             = 65536
  switch_name           = "Default Switch"
  headless              = true

  boot_wait    = "1s"
  boot_command = local.any_key

  communicator   = "winrm"
  winrm_username = var.admin_user
  winrm_password = var.admin_password
  winrm_timeout  = "2h"
  # The last provisioner disables the build admin, so Packer can't log on to shut down: the guest shuts itself down
  # and Packer only waits for Off.
  disable_shutdown = true
  shutdown_timeout = "30m"

  # The VM stays registered (Off) and is not exported: Hyper-V's fixed display and checkpoint settings live in the VM,
  # set by the post-processor below (as the earlier draft and new-vm.ps1 did).
  keep_registered  = true
  skip_export      = true
  output_directory = "runs/packer/${var.hyperv_vm_name}" # must not exist yet
}

# Packer's generated inventory sets ansible_shell_type=powershell for WinRM; ansible-core 2.21's winrm plugin needs cmd
# (it sends the module as stdin to a cmd-launched bootstrap). With powershell the first module call's stdin never
# arrived: the guest's bootstrap waited for good, and with a 3600 s WinRM operation timeout the build stalled in "Bake
# in updates" for an hour. Extra vars override the inventory. connection_timeout 120 (operation 120 s, read 130 s): with
# pywinrm's 20/30 s a guest busy with Office's install and disk IO dropped the connection ("Read timed out"). The same
# arguments in every ansible provisioner below (build.Password only exists inside a build).
build {
  name    = "base"
  sources = ["source.proxmox-iso.base"]

  provisioner "ansible" {
    playbook_file    = "infra/ansible/base.yml"
    user             = var.admin_user
    use_proxy        = false
    ansible_env_vars = ["ANSIBLE_CONFIG=infra/ansible/ansible.cfg"]
    extra_arguments = [
      "-e", "hypervisor=proxmox",
      "-e", "ansible_connection=winrm ansible_shell_type=cmd ansible_winrm_transport=basic ansible_winrm_scheme=http ansible_port=5985 ansible_winrm_connection_timeout=120",
      "-e", "{\"ansible_password\": \"${build.Password}\"}", # JSON: the password's characters are validated above
    ]
  }
}

build {
  name    = "golden"
  sources = ["source.proxmox-clone.golden", "source.hyperv-iso.hyperv"]

  # Hyper-V is one stage: its updates come here.
  provisioner "ansible" {
    only             = ["hyperv-iso.hyperv"]
    playbook_file    = "infra/ansible/base.yml"
    user             = var.admin_user
    use_proxy        = false
    ansible_env_vars = ["ANSIBLE_CONFIG=infra/ansible/ansible.cfg"]
    extra_arguments = [
      "-e", "hypervisor=hyperv",
      "-e", "ansible_connection=winrm ansible_shell_type=cmd ansible_winrm_transport=basic ansible_winrm_scheme=http ansible_port=5985 ansible_winrm_connection_timeout=120",
      "-e", "{\"ansible_password\": \"${build.Password}\"}",
    ]
  }

  provisioner "ansible" {
    playbook_file    = "infra/ansible/golden.yml"
    user             = var.admin_user
    use_proxy        = false
    ansible_env_vars = ["ANSIBLE_CONFIG=infra/ansible/ansible.cfg"]
    extra_arguments = [
      "-e", "hypervisor=${source.type == "hyperv-iso" ? "hyperv" : "proxmox"}",
      "-e", "ansible_connection=winrm ansible_shell_type=cmd ansible_winrm_transport=basic ansible_winrm_scheme=http ansible_port=5985 ansible_winrm_connection_timeout=120",
      "-e", "{\"ansible_password\": \"${build.Password}\"}",
    ]
  }

  # Last (spec: sysprep run by Packer): the build admin gets a random password nobody knows and is disabled, so the
  # image carries no usable build credential (clones get a per-range admin from range-up.yml); then sysprep
  # generalizes with the clones' answer file the sysprep role staged and shuts the guest down. Sysprep runs as a
  # SYSTEM scheduled task, not in this WinRM shell: generalize removes the NIC, WinRM then tears the shell down and
  # took a child sysprep with it, half generalized (IMAGE_STATE_UNDEPLOYABLE). The answer file deletes the task on
  # the clones' first boot. skip_clean: nothing may come back over WinRM after this script.
  provisioner "powershell" {
    skip_clean = true
    # Windows PowerShell 5.1: RNGCryptoServiceProvider.GetBytes (RandomNumberGenerator.Fill is .NET Core only). Any
    # error, an all-zero buffer, or a read-back that isn't "disabled, password changed" exits non-zero: the build fails.
    inline = [
      "$ErrorActionPreference = 'Stop'",
      "try {",
      "  $s = 'C:\\Windows\\System32\\Sysprep'",
      "  $before = (Get-LocalUser '${var.admin_user}').PasswordLastSet",
      "  $b = New-Object byte[] 32",
      "  $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider",
      "  $rng.GetBytes($b); $rng.Dispose()",
      "  if (-not ($b | Where-Object { $_ -ne 0 })) { throw 'random password buffer is all zero' }",
      "  Set-LocalUser '${var.admin_user}' -Password (ConvertTo-SecureString ([Convert]::ToBase64String($b)) -AsPlainText -Force)",
      "  Disable-LocalUser '${var.admin_user}'",
      "  $u = Get-LocalUser '${var.admin_user}'",
      "  if ($u.Enabled) { throw 'build admin is still enabled' }",
      "  if (-not ($u.PasswordLastSet -gt $before)) { throw \"build admin password not changed (last set $($u.PasswordLastSet))\" }",
      "  $act = New-ScheduledTaskAction -Execute \"$s\\sysprep.exe\" -Argument \"/generalize /oobe /shutdown /quiet /unattend:$s\\ludius-unattend.xml\"",
      "  $trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30)",
      "  Register-ScheduledTask ludius-sysprep -Action $act -Trigger $trg -User 'NT AUTHORITY\\SYSTEM' -RunLevel Highest -Force | Out-Null",
      "} catch { Write-Error $_ -ErrorAction Continue; exit 1 }",
    ]
  }

  # Proxmox: wait for sysprep's shutdown (on the node, as the README runs Packer); a sysprep error leaves the guest up
  # and fails the build here. The builder's own shutdown of the stopped VM is then a no-op before the conversion.
  # Hyper-V waits for the guest's shutdown itself (disable_shutdown above).
  provisioner "shell-local" {
    only   = ["proxmox-clone.golden"]
    inline = ["for i in $(seq 360); do qm status ${var.template_vmid} | grep -q stopped && exit 0; sleep 5; done; echo 'sysprep did not shut the VM down within 30 min' >&2; exit 1"]
  }

  # Hyper-V only: fixed 1920x1080 display in VM config (spec section 4), no automatic checkpoints, Guest Service
  # Interface, DVDs ejected; on the registered, Off VM (as the earlier draft and new-vm.ps1 did).
  post-processor "shell-local" {
    only               = ["hyperv-iso.hyperv"]
    execute_command    = ["pwsh", "-NoProfile", "-NonInteractive", "-File", "{{.Script}}"]
    tempfile_extension = ".ps1"
    inline = [
      "$ErrorActionPreference = 'Stop'",
      "$vm = Get-VM -Name '${var.hyperv_vm_name}'",
      "Set-VM $vm -AutomaticCheckpointsEnabled $false -CheckpointType Standard",
      "Set-VMVideo $vm -ResolutionType Single -HorizontalResolution 1920 -VerticalResolution 1080",
      "Enable-VMIntegrationService $vm -Name 'Guest Service Interface'",
      "Get-VMDvdDrive $vm | Where-Object Path | Set-VMDvdDrive -Path $null",
    ]
  }
}
