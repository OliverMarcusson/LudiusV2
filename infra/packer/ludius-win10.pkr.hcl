# UNVALIDATED DRAFT: Packer is not installed on this host and nothing here has been init'ed, validated or built.
# Packer is BUSL 1.1; installing it and fetching the plugin are the owner's call. Until a build has run, the working
# build is infra/hyperv/new-vm.ps1 + provision.ps1, and this file only mirrors it.
#
# Same image as new-vm.ps1 + provision.ps1 (spec section 7), with Packer creating, starting and stopping the VM:
#   - the install is unattended from the answer ISO new-vm.ps1 already wrote (infra/hyperv/build/answer.iso, gitignored,
#     filled with .env's credentials), so no password is in this file, a variable or Packer's log;
#   - no guest transport: communicator "none". Provisioning is provision.ps1 run on the host (shell-local) over
#     PowerShell Direct (VMBus, .env's credentials), the same path the existing build uses. WinRM and sshd stay off;
#   - provision.ps1 -WaitMinutes waits for setup to finish (persona logged on), since Packer has nothing to wait on;
#   - the guest is shut down from the host (Stop-VM, a graceful integration-service shutdown) and Packer only waits
#     (disable_shutdown), so it never hard-powers it off.
#
# No-build checks (touch no VM, no disk image):
#   packer fmt -check infra/packer
#   packer validate -syntax-only infra/packer
#   packer init infra/packer                     # downloads the pinned plugin: owner's decision
#   packer validate -var iso_checksum=<sha256 of the ISO> infra/packer
# Build (creates and starts a VM, writes a 64 GB dynamic VHDX and may copy the ISO into Packer's cache: never while
# the disk freeze holds; run from the repo root with every other ludius-* VM Off or Saved; the one-VM lock is held for
# the whole build so the host agent can't start another VM meanwhile). The guest gets the source VM's computer name
# and accounts (same answer ISO), so never put both on one domain:
#   . infra/hyperv/one-vm.ps1; Invoke-OneVm ludius-win10-pk { packer build -var iso_checksum=<sha256> infra/packer }
#
# Known differences from new-vm.ps1, closed by the post-processor below on the registered, Off VM: fixed 1920x1080
# video, no automatic checkpoints, Guest Service Interface, DVDs ejected. provision.ps1 runs with -NoCheckpoint: the
# "clean" checkpoint is the operator's to take afterwards. Relative paths assume packer runs from the repo root.

packer {
  required_version = ">= 1.11.0"
  required_plugins {
    hyperv = {
      source  = "github.com/hashicorp/hyperv"
      version = "= 1.1.5" # latest at drafting (2026-10-26), pinned exactly
    }
  }
}

variable "vm_name" {
  type    = string
  default = "ludius-win10-pk"
  validation {
    condition     = can(regex("^ludius-[A-Za-z0-9-]+$", var.vm_name))
    error_message = "A range VM name: ludius-*, never an existing VM (provision.ps1 targets it by name)."
  }
}

variable "iso_path" {
  type        = string
  default     = "${env("USERPROFILE")}/Downloads/win10.iso" # new-vm.ps1's -Iso default; a local file, never a download
  description = "Windows 10 Pro install ISO already on disk."
}

variable "iso_checksum" {
  type        = string
  description = "sha256 of iso_path (Get-FileHash). No default: an unchecked ISO is never built."
}

variable "answer_iso" {
  type        = string
  default     = "infra/hyperv/build/answer.iso"
  description = "Filled answer ISO from new-vm.ps1; must hold the same credentials as .env."
}

variable "output_directory" {
  type    = string
  default = "runs/packer/ludius-win10-pk" # must not exist yet (Packer refuses a non-empty one)
}

source "hyperv-iso" "win10" {
  vm_name              = var.vm_name
  iso_url              = var.iso_path
  iso_checksum         = "sha256:${var.iso_checksum}"
  secondary_iso_images = [var.answer_iso] # cd_files would need oscdimg/xorriso/mkisofs; none is installed

  generation            = 2
  enable_secure_boot    = true
  secure_boot_template  = "MicrosoftWindows"
  cpus                  = 4
  memory                = 6144
  enable_dynamic_memory = false
  disk_size             = 65536
  switch_name           = "Default Switch"
  headless              = true

  # The DVD loader's "press any key", answered like new-vm.ps1 does (Enter, every 500 ms).
  boot_wait    = "1s"
  boot_command = ["<enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter><wait500ms><enter>"]

  communicator     = "none"
  disable_shutdown = true # the last provisioner stops the VM; Packer waits for Off
  shutdown_timeout = "15m"

  # The VM stays registered (Off) and is not exported: no second 64 GB copy. Where its files land (temp_path/
  # output_directory) is per the plugin docs, unconfirmed here.
  keep_registered  = true
  skip_export      = true
  output_directory = var.output_directory
}

build {
  sources = ["source.hyperv-iso.win10"]

  provisioner "shell-local" {
    execute_command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", "{{.Script}}"]
    tempfile_extension = ".ps1"
    inline = [
      "$ErrorActionPreference = 'Stop'",
      "& infra/hyperv/provision.ps1 -VM '${var.vm_name}' -NoCheckpoint -WaitMinutes 120",
      "Stop-VM -Name '${var.vm_name}'", # graceful, through the shutdown integration service
    ]
  }

  post-processor "shell-local" {
    execute_command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", "{{.Script}}"]
    tempfile_extension = ".ps1"
    inline = [
      "$ErrorActionPreference = 'Stop'",
      "$vm = Get-VM -Name '${var.vm_name}'",
      "Set-VM $vm -AutomaticCheckpointsEnabled $false -CheckpointType Standard",
      "Set-VMVideo $vm -ResolutionType Single -HorizontalResolution 1920 -VerticalResolution 1080",
      "Enable-VMIntegrationService $vm -Name 'Guest Service Interface'",
      "Get-VMDvdDrive $vm | Where-Object Path | Set-VMDvdDrive -Path $null",
    ]
  }
}
