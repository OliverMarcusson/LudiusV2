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
sealed partial class HyperV(Guid observerService)
{
    static readonly ManagementScope Scope = new(@"\\.\root\virtualization\v2");

    [GeneratedRegex(@"^[\w .-]{1,64}$")]
    private static partial Regex SafeName();

    public sealed class Vm
    {
        public required string Name;
        public required Guid Id;
        public required ManagementObject System;
        public required ManagementObject Keyboard;
        public required ManagementObject Mouse;
        public required GuestChannel Channel;
        public readonly SemaphoreSlim InputGate = new(1, 1);
        public (int X, int Y)? Pointer;
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
                Channel = new GuestChannel(id, observerService),
            };
            vms[name] = vm;
            return vm;
        }
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
        var from = vm.Pointer;
        int steps = from is null || durationMs < 30 ? 1 : (int)Math.Clamp(durationMs / 15, 2, 60);
        // A slightly curved, eased path rather than a teleport or a ruler line.
        double bend = (Random.Shared.NextDouble() - 0.5) * 0.25;
        for (int i = 1; i <= steps; i++)
        {
            double t = (double)i / steps, e = t * t * (3 - 2 * t);
            int px = x, py = y;
            if (from is { } f && i < steps)
            {
                double dx = x - f.X, dy = y - f.Y;
                double arc = Math.Sin(Math.PI * t) * bend;
                // Stay inside the box spanned by start and target: Hyper-V rejects off-screen points (32773)
                // and ignores (0, 0).
                px = Math.Max(1, Math.Clamp((int)Math.Round(f.X + dx * e - dy * arc), Math.Min(f.X, x), Math.Max(f.X, x)));
                py = Math.Max(1, Math.Clamp((int)Math.Round(f.Y + dy * e + dx * arc), Math.Min(f.Y, y), Math.Max(f.Y, y)));
            }
            Call(vm.Mouse, "SetAbsolutePosition", px, py);
            if (i < steps) await Task.Delay((int)(durationMs / steps), ct);
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
            // Heavy-ish tail: mostly near the mean, sometimes a pause.
            double delay = meanMs + (Random.Shared.NextDouble() * 2 - 1) * jitterMs;
            if (Random.Shared.NextDouble() < 0.04) delay += meanMs * (2 + Random.Shared.NextDouble() * 4);
            if (delay > 0) await Task.Delay((int)delay, ct);
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

    public async Task<string> Lifecycle(string vm, Ludius.V1.LifecycleRequest.Types.Op op, string checkpoint, CancellationToken ct)
    {
        if (!SafeName().IsMatch(vm) || (checkpoint != "" && !SafeName().IsMatch(checkpoint))) throw new ArgumentException("invalid name");
        // Stop: a failing cmdlet (e.g. Restore-VMSnapshot of a checkpoint that doesn't exist) must fail the call;
        // otherwise the trailing state query exits 0 and a run silently continues on the unrestored VM.
        var script = "$ErrorActionPreference = 'Stop'; " + op switch
        {
            Ludius.V1.LifecycleRequest.Types.Op.Start => "Start-VM -Name $env:LUDIUS_VM",
            Ludius.V1.LifecycleRequest.Types.Op.Stop => "Stop-VM -Name $env:LUDIUS_VM -Force",
            Ludius.V1.LifecycleRequest.Types.Op.Checkpoint => "Checkpoint-VM -Name $env:LUDIUS_VM -SnapshotName $env:LUDIUS_CP",
            Ludius.V1.LifecycleRequest.Types.Op.Restore => "Restore-VMSnapshot -VMName $env:LUDIUS_VM -Name $env:LUDIUS_CP -Confirm:$false",
            _ => "",
        } + "; (Get-VM -Name $env:LUDIUS_VM).State.ToString()";
        if (op != Ludius.V1.LifecycleRequest.Types.Op.State) Forget(vm);
        var psi = new ProcessStartInfo("pwsh", ["-NoProfile", "-NonInteractive", "-Command", script])
        {
            RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false,
            Environment = { ["LUDIUS_VM"] = vm, ["LUDIUS_CP"] = checkpoint },
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

    public sealed record VmInfo(string Name, string State, int Width, int Height);

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
