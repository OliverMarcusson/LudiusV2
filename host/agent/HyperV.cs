using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Management;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;

namespace Ludius.HostAgent;

// Hyper-V backend: synthetic keyboard and mouse through WMI (root\virtualization\v2),
// lifecycle through the Hyper-V PowerShell module, console frames through the
// thumbnail API. Coordinates are guest physical pixels.
sealed partial class HyperV(Guid observerService, string oneVmScript, int maxRunning = 1) : IHypervisor
{
    public string Backend => "hyperv";
    VmHandle IHypervisor.Get(string name) => Get(name);
    Task IHypervisor.Move(VmHandle vm, int x, int y, uint durationMs, CancellationToken ct) => Move((Vm)vm, x, y, durationMs, ct);
    Task IHypervisor.Click(VmHandle vm, int button, uint count, CancellationToken ct) => Click((Vm)vm, button, count, ct);
    void IHypervisor.Key(VmHandle vm, uint vk, bool down) => Key((Vm)vm, vk, down);
    Task IHypervisor.Chord(VmHandle vm, IReadOnlyList<uint> keys, CancellationToken ct) => Chord((Vm)vm, keys, ct);
    Task IHypervisor.Type(VmHandle vm, string text, uint meanMs, uint jitterMs, CancellationToken ct) => Type((Vm)vm, text, meanMs, jitterMs, ct);
    void IHypervisor.CtrlAltDel(VmHandle vm) => CtrlAltDel((Vm)vm);
    Task IHypervisor.Scroll(VmHandle vm, int notches, CancellationToken ct) => Scroll((Vm)vm, notches, ct);
    (byte[] Png, int Width, int Height) IHypervisor.Screenshot(VmHandle vm) => Screenshot((Vm)vm);

    static readonly ManagementScope Scope = new(@"\\.\root\virtualization\v2");

    [GeneratedRegex(@"^[\w .-]{1,64}$")]
    private static partial Regex SafeName();

    public sealed class Vm : VmHandle
    {
        public required Guid Id;
        public required ManagementObject System;
        public required ManagementObject Keyboard;
        public required ManagementObject Mouse;
    }

    readonly Dictionary<string, Vm> vms = [];

    public Vm Get(string name)
    {
        lock (vms)
        {
            if (vms.TryGetValue(name, out var vm)) return vm;
            if (!SafeName().IsMatch(name)) throw new ArgumentException("invalid VM name");
            var system = Query($"SELECT * FROM Msvm_ComputerSystem WHERE ElementName='{name}'").SingleOrDefault()
                ?? throw new KeyNotFoundException($"no VM named {name}");
            var id = Guid.Parse((string)system["Name"]);
            vm = new Vm
            {
                Name = name,
                Id = id,
                System = system,
                Keyboard = Related(system, "Msvm_Keyboard"),
                Mouse = Related(system, "Msvm_SyntheticMouse"),
                Channel = new GuestChannel(ct => Connect(id, ct)),
            };
            vms[name] = vm;
            return vm;
        }
    }

    async Task<System.Net.Sockets.Socket> Connect(Guid vmId, CancellationToken ct)
    {
        var s = new System.Net.Sockets.Socket(Ludius.Observer.HyperVEndPoint.Family, System.Net.Sockets.SocketType.Stream, Ludius.Observer.HyperVEndPoint.RawProtocol);
        try
        {
            // ConnectEx (ConnectAsync) is TCP-only: WSAEINVAL on AF_HYPERV.
            await Task.Run(() => s.Connect(new Ludius.Observer.HyperVEndPoint(vmId, observerService)), ct).WaitAsync(ct);
            return s;
        }
        catch { s.Dispose(); throw; }
    }

    // Lifecycle changes invalidate device handles and the guest channel.
    public void Forget(string name)
    {
        lock (vms)
        {
            if (vms.Remove(name, out var vm)) vm.Channel.Dispose();
        }
    }

    static IEnumerable<ManagementObject> Query(string wql) =>
        new ManagementObjectSearcher(Scope, new ObjectQuery(wql)).Get().Cast<ManagementObject>();

    static ManagementObject Related(ManagementObject o, string cls) =>
        o.GetRelated(cls).Cast<ManagementObject>().SingleOrDefault() ?? throw new InvalidOperationException($"{cls} not available (VM off?)");

    static void Call(ManagementObject o, string method, params object[] args)
    {
        object rc;
        // WMI occasionally fails a call with a bare COM status (0xC0000002 seen mid-TypeText). A failed call sent
        // nothing to the guest, so retrying it can't double a keystroke.
        for (int attempt = 1; ; attempt++)
        {
            try { rc = o.InvokeMethod(method, args); break; }
            catch (System.Runtime.InteropServices.COMException) when (attempt < 3) { Thread.Sleep(200); }
        }
        if (rc is uint code && code != 0) throw new InvalidOperationException($"{method} returned {code}");
        if (rc is bool ok && !ok) throw new InvalidOperationException($"{method} failed");
    }

    // Input --------------------------------------------------------------

    public async Task Move(Vm vm, int x, int y, uint durationMs, CancellationToken ct)
    {
        int steps = Human.Steps(vm.Pointer, durationMs), i = 0;
        foreach (var (px, py) in Human.Path(vm.Pointer, x, y, steps))
        {
            Call(vm.Mouse, "SetAbsolutePosition", px, py);
            if (++i < steps) await Task.Delay((int)(durationMs / steps), ct);
        }
        vm.Pointer = (x, y);
    }

    // ButtonIndex: 1 left, 2 right, 3 middle (verified by the contract test).
    public async Task Click(Vm vm, int button, uint count, CancellationToken ct)
    {
        for (uint i = 0; i < Math.Max(1, count); i++)
        {
            Call(vm.Mouse, "SetButtonState", (uint)button, true);
            await Task.Delay(Random.Shared.Next(45, 95), ct);
            Call(vm.Mouse, "SetButtonState", (uint)button, false);
            if (i + 1 < count) await Task.Delay(Random.Shared.Next(70, 130), ct);
        }
    }

    public void Key(Vm vm, uint vk, bool down) => Call(vm.Keyboard, down ? "PressKey" : "ReleaseKey", vk);

    public async Task Chord(Vm vm, IReadOnlyList<uint> keys, CancellationToken ct)
    {
        foreach (var k in keys) { Key(vm, k, true); await Task.Delay(Random.Shared.Next(20, 50), ct); }
        for (int i = keys.Count - 1; i >= 0; i--) { Key(vm, keys[i], false); await Task.Delay(Random.Shared.Next(15, 35), ct); }
    }

    public async Task Type(Vm vm, string text, uint meanMs, uint jitterMs, CancellationToken ct)
    {
        if (text.Any(c => c is not ('\n' or '\t') && (c < ' ' || c > '~')))
            throw new ArgumentException("only printable ASCII, \\n and \\t can be typed");
        foreach (var c in text)
        {
            switch (c)
            {
                case '\n': Call(vm.Keyboard, "TypeKey", 0x0Du); break;
                case '\t': Call(vm.Keyboard, "TypeKey", 0x09u); break;
                default: Call(vm.Keyboard, "TypeText", c.ToString()); break;
            }
            if (Human.KeyDelay(meanMs, jitterMs) is var delay and > 0) await Task.Delay(delay, ct);
        }
    }

    public void CtrlAltDel(Vm vm) => Call(vm.Keyboard, "TypeCtrlAltDel");

    public async Task Scroll(Vm vm, int notches, CancellationToken ct)
    {
        for (int i = 0; i < Math.Abs(notches); i++)
        {
            Call(vm.Mouse, "SetScrollPosition", Math.Sign(notches) * 120);
            await Task.Delay(Random.Shared.Next(40, 110), ct);
        }
    }

    // Lifecycle (PowerShell, as in spec section 4) --------------------------

    // Clone: export the checkpoint into a scratch folder next to the new VM, import a copy with a new VM id, then
    // remove only that scratch export. The source VM and its checkpoints are untouched.
    const string CloneScript = """
        if (Get-VM -Name $env:LUDIUS_TARGET -ErrorAction SilentlyContinue) { throw "a VM named $env:LUDIUS_TARGET exists" }
        $dest = Join-Path (Get-VMHost).VirtualHardDiskPath $env:LUDIUS_TARGET
        if (Test-Path $dest) { throw "$dest exists" }
        $scratch = Join-Path $dest 'export'
        Export-VMSnapshot -VMName $env:LUDIUS_VM -Name $env:LUDIUS_CP -Path $scratch
        $vmcx = Get-ChildItem $scratch -Recurse -Filter *.vmcx | Where-Object FullName -like '*Virtual Machines*' | Select-Object -First 1
        $new = Import-VM -Path $vmcx.FullName -Copy -GenerateNewId -VhdDestinationPath $dest -VirtualMachinePath $dest -SnapshotFilePath $dest -SmartPagingFilePath $dest
        Rename-VM -VM $new -NewName $env:LUDIUS_TARGET
        Remove-Item -LiteralPath $scratch -Recurse -Force
        $env:LUDIUS_VM = $env:LUDIUS_TARGET
        """;

    // Checkpoint: never under an existing name. Hyper-V accepts duplicates, and a second "clean" makes every later
    // Restore-VMSnapshot -Name clean fail as ambiguous; a baseline is never replaced either (provision.ps1 alike).
    const string CheckpointScript = """
        if (Get-VMSnapshot -VMName $env:LUDIUS_VM -Name $env:LUDIUS_CP -ErrorAction SilentlyContinue) { throw "checkpoint '$env:LUDIUS_CP' exists on $env:LUDIUS_VM and is never duplicated or replaced: use a new name" }
        Checkpoint-VM -Name $env:LUDIUS_VM -SnapshotName $env:LUDIUS_CP
        """;

    // Delete: merge checkpoints, unregister, then remove the VM's current disks. Only ludius-* VMs, only with the
    // name repeated as confirmation.
    const string DeleteScript = """
        Stop-VM -Name $env:LUDIUS_VM -TurnOff -Force
        Get-VMSnapshot -VMName $env:LUDIUS_VM | Remove-VMSnapshot
        $disks = (Get-VMHardDiskDrive -VMName $env:LUDIUS_VM).Path
        Remove-VM -Name $env:LUDIUS_VM -Force
        $disks | ForEach-Object { Remove-Item -LiteralPath $_ -Force }
        'Deleted'
        """;

    // Host's cap (Agent:MaxRunningVms, default 1: the owner's limit on this machine). Start and restore (a checkpoint of a running VM comes back running) go
    // through infra/hyperv/one-vm.ps1, which rechecks and activates under a host-wide lock shared with the scripts.
    // A missing script fails the call: no activation without the lock.
    static string OneVm(string activate) => $". $env:LUDIUS_ONEVM; Invoke-OneVm $env:LUDIUS_VM {{ {activate} }} -Max $env:LUDIUS_MAXVMS";

    public async Task<string> Lifecycle(string vm, Ludius.V1.LifecycleRequest.Types.Op op, string checkpoint, string target, CancellationToken ct)
    {
        if (!SafeName().IsMatch(vm) || (checkpoint != "" && !SafeName().IsMatch(checkpoint)) || (target != "" && !SafeName().IsMatch(target)))
            throw new ArgumentException("invalid name");
        if (op == Ludius.V1.LifecycleRequest.Types.Op.Clone && (checkpoint == "" || !target.StartsWith("ludius-")))
            throw new ArgumentException("clone needs a checkpoint and a target named ludius-*");
        if (op is Ludius.V1.LifecycleRequest.Types.Op.Checkpoint or Ludius.V1.LifecycleRequest.Types.Op.Restore && checkpoint == "")
            throw new ArgumentException("checkpoint and restore need a checkpoint name");
        if (op == Ludius.V1.LifecycleRequest.Types.Op.Delete && (!vm.StartsWith("ludius-") || target != vm))
            throw new ArgumentException("delete is limited to ludius-* VMs and needs the VM name repeated as confirmation");
        // Stop: a failing cmdlet (e.g. Restore-VMSnapshot of a checkpoint that doesn't exist) must fail the call;
        // otherwise the trailing state query exits 0 and a run silently continues on the unrestored VM.
        var script = "$ErrorActionPreference = 'Stop'; " + op switch
        {
            Ludius.V1.LifecycleRequest.Types.Op.Start => OneVm("Start-VM -Name $env:LUDIUS_VM"),
            Ludius.V1.LifecycleRequest.Types.Op.Stop => "Stop-VM -Name $env:LUDIUS_VM -Force",
            Ludius.V1.LifecycleRequest.Types.Op.Checkpoint => CheckpointScript,
            Ludius.V1.LifecycleRequest.Types.Op.Restore => OneVm("Restore-VMSnapshot -VMName $env:LUDIUS_VM -Name $env:LUDIUS_CP -Confirm:$false"),
            Ludius.V1.LifecycleRequest.Types.Op.Clone => CloneScript,
            Ludius.V1.LifecycleRequest.Types.Op.Delete => DeleteScript,
            _ => "",
        } + (op == Ludius.V1.LifecycleRequest.Types.Op.Delete ? "" : "; (Get-VM -Name $env:LUDIUS_VM).State.ToString()");
        if (op != Ludius.V1.LifecycleRequest.Types.Op.State) Forget(vm);
        var psi = new ProcessStartInfo("pwsh", ["-NoProfile", "-NonInteractive", "-Command", script])
        {
            RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false,
            Environment = { ["LUDIUS_VM"] = vm, ["LUDIUS_CP"] = checkpoint, ["LUDIUS_TARGET"] = target, ["LUDIUS_ONEVM"] = oneVmScript, ["LUDIUS_MAXVMS"] = $"{maxRunning}" },
        };
        using var p = Process.Start(psi)!;
        var stdout = p.StandardOutput.ReadToEndAsync(ct);
        var stderr = p.StandardError.ReadToEndAsync(ct);
        await p.WaitForExitAsync(ct);
        if (p.ExitCode != 0) throw new InvalidOperationException((await stderr).Trim());
        return (await stdout).Trim().Split('\n')[^1].Trim();
    }

    // Console frame ----------------------------------------------------------

    public (byte[] Png, int Width, int Height) Screenshot(Vm vm)
    {
        var (w, h) = Resolution(vm.System);
        var settings = Query($"ASSOCIATORS OF {{{vm.System.Path.RelativePath}}} WHERE ResultClass=Msvm_VirtualSystemSettingData")
            .First(s => (string)s["VirtualSystemType"] == "Microsoft:Hyper-V:System:Realized");
        using var bmp = Thumbnail(settings.Path.Path, w, h);
        using var ms = new MemoryStream();
        bmp.Save(ms, ImageFormat.Png);
        return (ms.ToArray(), w, h);
    }

    // Monitoring (view only) ------------------------------------------------

    // Every VM on the host: Msvm_ComputerSystem rows named by GUID (the host's own row carries its computer name).
    public List<VmInfo> ListVms() =>
        Query("SELECT * FROM Msvm_ComputerSystem")
            .Where(s => Guid.TryParse((string)s["Name"], out _))
            .Select(s =>
            {
                var state = StateName(Convert.ToInt32(s["EnabledState"]));
                var (w, h) = state == "running" ? Resolution(s) : (0, 0);
                return new VmInfo((string)s["ElementName"], state, w, h);
            })
            .OrderBy(v => v.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();

    public static bool ValidName(string name) => SafeName().IsMatch(name);

    // Per VM, cached between frames: the realized settings path the thumbnail call needs and the guest resolution
    // (re-read every 5 s). Without the cache every frame paid ~350 ms of WMI queries; the call itself is ~20 ms.
    sealed class Screen
    {
        public required ManagementObject System;
        public required string Settings;
        public int W, H;
        public DateTime ResolvedAt;
    }

    readonly Dictionary<string, Screen> screens = [];
    static readonly ThreadLocal<ManagementObject> Service = new(() => Query("SELECT * FROM Msvm_VirtualSystemManagementService").Single());
    static readonly ImageCodecInfo JpegCodec = ImageCodecInfo.GetImageEncoders().First(c => c.FormatID == ImageFormat.Jpeg.Guid);

    // One console frame as JPEG, scaled by Hyper-V to at most maxWidth. Read-only: never touches input.
    public byte[] Frame(string name, int maxWidth)
    {
        if (!ValidName(name)) throw new ArgumentException("invalid VM name");
        var s = ScreenOf(name);
        int tw = Math.Clamp(maxWidth, 160, s.W) & ~1; // even width: RGB565 rows then need no stride padding
        int th = Math.Max(2, (int)Math.Round((double)s.H * tw / s.W)) & ~1;
        try
        {
            using var bmp = Thumbnail(s.Settings, tw, th);
            using var ms = new MemoryStream();
            using var quality = new EncoderParameters(1);
            quality.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 80L);
            bmp.Save(ms, JpegCodec, quality);
            return ms.ToArray();
        }
        catch
        {
            lock (screens) screens.Remove(name);
            s.System.Get();
            if (StateName(Convert.ToInt32(s.System["EnabledState"])) != "running") throw new InvalidOperationException("VM is not running");
            throw;
        }
    }

    Screen ScreenOf(string name)
    {
        lock (screens)
        {
            if (screens.TryGetValue(name, out var s) && DateTime.UtcNow - s.ResolvedAt < TimeSpan.FromSeconds(5)) return s;
            if (s is null)
            {
                var system = Query($"SELECT * FROM Msvm_ComputerSystem WHERE ElementName='{name}'").SingleOrDefault()
                    ?? throw new KeyNotFoundException($"no VM named {name}");
                if (StateName(Convert.ToInt32(system["EnabledState"])) != "running") throw new InvalidOperationException("VM is not running");
                var settings = Query($"ASSOCIATORS OF {{{system.Path.RelativePath}}} WHERE ResultClass=Msvm_VirtualSystemSettingData")
                    .First(x => (string)x["VirtualSystemType"] == "Microsoft:Hyper-V:System:Realized");
                s = new Screen { System = system, Settings = settings.Path.Path };
            }
            (s.W, s.H) = Resolution(s.System);
            s.ResolvedAt = DateTime.UtcNow;
            screens[name] = s;
            return s;
        }
    }

    static string StateName(int s) => s switch
    {
        2 => "running",
        3 => "off",
        6 or 32769 => "saved",
        9 or 32768 => "paused",
        10 or 32770 => "starting",
        4 or 32771 => "stopping",
        32773 or 32774 => "saving",
        _ => "other",
    };

    static (int W, int H) Resolution(ManagementObject system)
    {
        var head = Query($"ASSOCIATORS OF {{{system.Path.RelativePath}}} WHERE ResultClass=Msvm_VideoHead").FirstOrDefault();
        return head is null ? (1920, 1080)
            : (Convert.ToInt32(head["CurrentHorizontalResolution"]), Convert.ToInt32(head["CurrentVerticalResolution"]));
    }

    static Bitmap Thumbnail(string settingsPath, int w, int h)
    {
        var svc = Service.Value!; // one per thread: overlapping captures don't share a WMI object
        var inParams = svc.GetMethodParameters("GetVirtualSystemThumbnailImage");
        inParams["TargetSystem"] = settingsPath; // a REF parameter: the object path
        inParams["WidthPixels"] = (ushort)w;
        inParams["HeightPixels"] = (ushort)h;
        var outParams = svc.InvokeMethod("GetVirtualSystemThumbnailImage", inParams, null);
        if ((uint)outParams["ReturnValue"] != 0) throw new InvalidOperationException("thumbnail failed");
        var data = (byte[])outParams["ImageData"];
        var bmp = new Bitmap(w, h, PixelFormat.Format16bppRgb565);
        var bits = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.WriteOnly, bmp.PixelFormat);
        Marshal.Copy(data, 0, bits.Scan0, Math.Min(data.Length, bits.Stride * h));
        bmp.UnlockBits(bits);
        return bmp;
    }
}
