
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
  default = "root@pam!packer"
}

variable "proxmox_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "template_vmid" {
  type    = number
  default = 9000
}

variable "base_vmid" {
  type    = number
  default = 8999
}

variable "range_pool" {
  type    = string
  default = "ludius"
}

variable "storage" {
  type    = string
  default = "local-lvm"
}

variable "build_bridge" {
  type    = string
  default = "vmbr2"
}

variable "hyperv_iso_path" {
  type    = string
  default = "${env("USERPROFILE")}/Downloads/win10.iso"
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
  any_key = ["<enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter>"]
}

source "proxmox-iso" "base" {
  proxmox_url              = var.proxmox_url
  insecure_skip_tls_verify = true
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
    iso_file = "local:iso/virtio-win-0.1.302.iso"
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
  cpu_type = "x86-64-v3"
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
  disable_shutdown = true
  shutdown_timeout = "30m"

  keep_registered  = true
  skip_export      = true
  output_directory = "runs/packer/${var.hyperv_vm_name}"
}

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
      "-e", "{\"ansible_password\": \"${build.Password}\"}",
    ]
  }
}

build {
  name    = "golden"
  sources = ["source.proxmox-clone.golden", "source.hyperv-iso.hyperv"]

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

  provisioner "powershell" {
    skip_clean = true
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

  provisioner "shell-local" {
    only   = ["proxmox-clone.golden"]
    inline = ["for i in $(seq 360); do qm status ${var.template_vmid} | grep -q stopped && exit 0; sleep 5; done; echo 'sysprep did not shut the VM down within 30 min' >&2; exit 1"]
  }

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
