param([string]$VMName = 'ludius-win10-01', [string]$Out = "$PSScriptRoot\build\screen.png", [int]$Width = 1920, [int]$Height = 1080)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$ns = 'root\virtualization\v2'
$vm = Get-CimInstance -Namespace $ns -ClassName Msvm_ComputerSystem -Filter "ElementName='$VMName'"
$settings = Get-CimAssociatedInstance $vm -ResultClassName Msvm_VirtualSystemSettingData | Where-Object VirtualSystemType -eq 'Microsoft:Hyper-V:System:Realized'
$svc = Get-CimInstance -Namespace $ns -ClassName Msvm_VirtualSystemManagementService
$r = Invoke-CimMethod -InputObject $svc -MethodName GetVirtualSystemThumbnailImage -Arguments @{ TargetSystem = $settings; WidthPixels = [uint16]$Width; HeightPixels = [uint16]$Height }
if ($r.ReturnValue -ne 0) { throw "thumbnail failed: $($r.ReturnValue)" }
$bmp = New-Object Drawing.Bitmap $Width, $Height, ([Drawing.Imaging.PixelFormat]::Format16bppRgb565)
$data = $bmp.LockBits((New-Object Drawing.Rectangle 0, 0, $Width, $Height), 'WriteOnly', $bmp.PixelFormat)
[Runtime.InteropServices.Marshal]::Copy([byte[]]$r.ImageData, 0, $data.Scan0, $r.ImageData.Length)
$bmp.UnlockBits($data)
$bmp.Save($Out, [Drawing.Imaging.ImageFormat]::Png)
$Out
