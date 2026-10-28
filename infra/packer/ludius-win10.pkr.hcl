# Golden Windows 10 image (spec section 7): one Ansible playbook (infra/ansible/golden.yml) applied by Packer with the
# proxmox-iso builder (the range: a Proxmox template for linked clones) and the hyperv-iso builder (parity with the
# Hyper-V test host: a generalized VHDX). Same answer file for both (autounattend.xml.pkrtpl, built into a CD by
# Packer, so no password is in a committed file), WinRM for Packer and Ansible during the build, sysprep last as a
# Packer provisioner. Persona accounts, domain join and the observer's logon task are per clone (range-up.yml).
#
# Run from the repo root (relative paths below), with ANSIBLE_VAULT_PASSWORD_FILE set and PKR_VAR_admin_password set
# (a build-only password: the build admin is disabled with a random password at the end):
#   packer init infra/packer
#   packer build -only=proxmox-iso.proxmox -var proxmox_token=$(cat /root/.ludius-packer-token) \
#     -var iso_checksum=<sha256 of win10.iso> infra/packer                          # on the Proxmox node
# Hyper-V (desktop): needs oscdimg (Windows ADK) for the answer CD and an Ansible control node, which Windows itself
# can't be; the build creates and starts a VM, so only with the owner's OK and every other ludius-* VM Off or Saved:
#   . infra/hyperv/one-vm.ps1; Invoke-OneVm ludius-win10-pk { packer build -only=hyperv-iso.hyperv ... infra/packer }

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

source "proxmox-iso" "proxmox" {
  proxmox_url              = var.proxmox_url
  insecure_skip_tls_verify = true # the node's own self-signed API certificate, on loopback
  username                 = var.proxmox_token_id
  token                    = var.proxmox_token
  node                     = var.proxmox_node
  vm_id                    = var.template_vmid
  vm_name                  = "ludius-win10-golden"
  template_description     = "Ludius golden Windows 10 (infra/packer), generalized"
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
    iso_file = "local:iso/virtio-win.iso"
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
  cpu_type = "host"
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

build {
  sources = ["source.proxmox-iso.proxmox", "source.hyperv-iso.hyperv"]

  provisioner "ansible" {
    playbook_file    = "infra/ansible/golden.yml"
    user             = var.admin_user
    use_proxy        = false
    ansible_env_vars = ["ANSIBLE_CONFIG=infra/ansible/ansible.cfg"]
    extra_arguments = [
      "-e", "hypervisor=${source.name}",
      "-e", "ansible_connection=winrm ansible_winrm_transport=basic ansible_winrm_scheme=http ansible_port=5985",
      "-e", "ansible_winrm_read_timeout_sec=3700 ansible_winrm_operation_timeout_sec=3600",
      "-e", "{\"ansible_password\": \"${build.Password}\"}", # JSON: the password's characters are validated above
    ]
  }

  # Last (spec: sysprep run by Packer): generalize with the clones' answer file the sysprep role staged, and /quit;
  # then the build admin gets a random password nobody knows and is disabled, so the image carries no usable build
  # credential (clones get a per-range admin from range-up.yml). Proxmox's builder then shuts the VM down through its
  # API and converts it to the template; on Hyper-V the guest shuts itself down (disable_shutdown above).
  provisioner "powershell" {
    inline = [
      "$s = 'C:\\Windows\\System32\\Sysprep'",
      "$a = '/generalize', '/oobe', '/quit', '/quiet', \"/unattend:$s\\ludius-unattend.xml\"",
      "$p = Start-Process \"$s\\sysprep.exe\" -ArgumentList $a -Wait -PassThru",
      "if ($p.ExitCode) { throw \"sysprep exit $($p.ExitCode); see $s\\Panther\\setupact.log\" }",
      "$b = [byte[]]::new(32); [Security.Cryptography.RandomNumberGenerator]::Fill($b)",
      "Set-LocalUser '${var.admin_user}' -Password (ConvertTo-SecureString ([Convert]::ToBase64String($b)) -AsPlainText -Force)",
      "Disable-LocalUser '${var.admin_user}'",
      "if ('${source.name}' -eq 'hyperv') { shutdown /s /t 20 /d p:4:1 /c Packer }",
    ]
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
