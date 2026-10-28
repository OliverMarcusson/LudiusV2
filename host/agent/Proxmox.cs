using System.Buffers.Binary;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Op = Ludius.V1.LifecycleRequest.Types.Op;

namespace Ludius.HostAgent;

// Proxmox backend (production, spec section 4), node-local: input and console frames through the VM's QMP socket
// (USB tablet, absolute 0-32767), the guest channel over vsock (CID from the VM's vhost-vsock device), lifecycle
// through the Proxmox API (linked clones from templates, snapshots). Coordinates are guest physical pixels.
// Confined to the range: every call (input, observation, lifecycle, clone sources) names a VM in the range's Proxmox
// pool (Proxmox:Pool) on this node, and clones are created into that pool. Anything else is refused, never touched.
sealed partial class Proxmox(IPveApi api, string node, ProxmoxOptions options) : IHypervisor
{
    const int AbsMax = 0x7FFF;

    public sealed class Vm : VmHandle
    {
        public required int Id;
        public required Qmp Qmp;
        public required string Identity; // see IdentityOf
        public DateTime CheckedAt;
    }

    readonly Dictionary<string, Vm> vms = [];
    readonly SemaphoreSlim activation = new(1, 1); // the cap's count and the start it allows, one at a time
    // Under the activation lock: VMIDs whose start or rollback this agent asked for but never saw confirmed (a cancelled
    // call; the Proxmox task may still be bringing the VM up). They count as running until a later count reads them
    // running (then they count by their state) or until ReservationTtl passes.
    readonly Dictionary<int, DateTime> reserved = [];

    // Under the vms lock: per VMID, the clock value of its last invalidation (delete, clone), and the VMIDs a clone is
    // still configuring (a count per VMID: each clone removes only its own mark). A lookup records the clock before it resolves and caches its handle only if its VMID was not
    // invalidated since and is not being configured: a lookup that raced a delete or clone never caches a stale handle.
    long clock;
    readonly Dictionary<int, long> invalidated = [];
    readonly Dictionary<int, int> configuring = [];

    bool ChangedSince(int id, long seen) => configuring.ContainsKey(id) || invalidated.GetValueOrDefault(id) > seen;

    static IOException Raced(string name, int id) =>
        new($"{name} (VM {id}) was deleted, cloned or reconfigured during the lookup: try again");

    // A VM's vsock CID is its VMID. A clone starts with its source's CID until Clone sets its own; a handle on a VM
    // with another VM's CID would read that VM's observer, so it is refused (whatever left it so: a failed or
    // cancelled clone, a hand edit). No CID at all is allowed: the channel then fails at connect.
    static uint? OwnCid(string name, int id, JsonNode? cfg)
    {
        var cid = Cid(cfg?["args"]?.GetValue<string>());
        return cid is null || cid == (uint)id ? cid
            : throw new IOException($"{name} (VM {id}) has vsock CID {cid}, not its VMID: refused until its CID is set");
    }

    // Tests replace the vsock connect and the clock.
    public Func<uint, CancellationToken, Task<Socket>> ConnectGuest = (cid, ct) => Vsock.Connect(cid, options.VsockPort, ct);
    public Func<DateTime> Now = () => DateTime.UtcNow;

    public string Backend => "proxmox";
    VmHandle IHypervisor.Get(string name) => Get(name);

    // The handle for input, observation, screenshots and file reads. A cached handle is trusted for options.Recheck
    // (a check per keystroke would cost a pvesh round trip each), then checked again: same VMID still in the range
    // pool on this node, same name, same identity. Otherwise it is dropped and the call refused: a range VM deleted and
    // its VMID reused by another VM is never reached through an old handle.
    public Vm Get(string name)
    {
        if (!VmName.Valid(name)) throw new ArgumentException("invalid VM name");
        Vm? cached;
        long seen;
        lock (vms)
        {
            vms.TryGetValue(name, out cached);
            seen = clock;
        }
        if (cached is not null)
        {
            if (Now() - cached.CheckedAt < options.Recheck) return cached;
            Recheck(cached);
            lock (vms)
                if (!ChangedSince(cached.Id, seen) && vms.TryGetValue(name, out var still) && still == cached) return cached;
            Forget(name);
            throw Raced(name, cached.Id);
        }
        var id = Find(name, CancellationToken.None).GetAwaiter().GetResult().Id;
        var cfg = Wait(api.Call("get", $"/nodes/{node}/qemu/{id}/config", null, CancellationToken.None));
        var vm = new Vm
        {
            Name = name,
            Id = id,
            Identity = IdentityOf(cfg),
            CheckedAt = Now(),
            Qmp = new Qmp(Path.Combine(options.RunDir, $"{id}.qmp")),
            Channel = new GuestChannel(ct => OwnCid(name, id, cfg) is { } c ? ConnectGuest(c, ct)
                : throw new IOException($"{name} has no vsock device (args: -device vhost-vsock-pci,guest-cid=N)")),
        };
        lock (vms)
        {
            if (ChangedSince(id, seen)) { vm.Channel.Dispose(); throw Raced(name, id); }
            try { OwnCid(name, id, cfg); } catch { vm.Channel.Dispose(); throw; }
            if (vms.TryGetValue(name, out var raced)) { vm.Channel.Dispose(); return raced; }
            return vms[name] = vm;
        }
    }

    static T Wait<T>(Task<T> t) => t.GetAwaiter().GetResult();
    static void Wait(Task t) => t.GetAwaiter().GetResult();

    void Recheck(Vm vm)
    {
        try
        {
            var now = Wait(List(CancellationToken.None)).Where(v => v.Id == vm.Id).ToList();
            if (now is not [var v]) throw new KeyNotFoundException($"{vm.Name} (VM {vm.Id}) is gone");
            if (v.Name != vm.Name || !InRange(v))
                throw new UnauthorizedAccessException($"VM {vm.Id} is no longer the range VM {vm.Name} (pool {options.Pool})");
            var cfg = Wait(api.Call("get", $"/nodes/{node}/qemu/{vm.Id}/config", null, CancellationToken.None));
            if (IdentityOf(cfg) != vm.Identity)
                throw new UnauthorizedAccessException($"VM {vm.Id} named {vm.Name} is a different VM than before (VMID reused)");
            OwnCid(vm.Name, vm.Id, cfg);
            vm.CheckedAt = Now();
        }
        catch
        {
            Forget(vm.Name); // fail closed: the next call resolves from scratch
            throw;
        }
    }

    // What tells two VMs with one VMID apart: Proxmox's creation stamp (meta: creation-qemu=...,ctime=...) and the
    // SMBIOS UUID, both new for every created or cloned VM and unchanged by snapshots, rollbacks and restarts.
    public static string IdentityOf(JsonNode? cfg) =>
        $"{cfg?["meta"]?.GetValue<string>()}|{(cfg?["smbios1"]?.GetValue<string>() is { } s ? SmbiosUuid().Match(s).Value : "")}";

    [GeneratedRegex(@"(?<=\buuid=)[0-9A-Fa-f-]+")]
    private static partial Regex SmbiosUuid();

    public void Forget(string name)
    {
        lock (vms) if (vms.Remove(name, out var vm)) vm.Channel.Dispose();
    }

    // Drops every cached handle on this VMID, whatever name it was cached under, and stamps the VMID so lookups
    // already under way don't cache theirs (delete, clone, a VMID about to be reused).
    void Invalidate(int id)
    {
        lock (vms)
        {
            invalidated[id] = ++clock;
            foreach (var (name, vm) in vms.Where(kv => kv.Value.Id == id).ToList())
            {
                vms.Remove(name);
                vm.Channel.Dispose();
            }
        }
    }

    [GeneratedRegex(@"vhost-vsock-pci\b[^ ]*\bguest-cid=(\d+)")]
    private static partial Regex CidArg();

    public static uint? Cid(string? args) => args is not null && CidArg().Match(args) is { Success: true } m ? uint.Parse(m.Groups[1].Value) : null;

    // The VM's args with its vsock CID set to `cid` (a clone keeps its source's CID otherwise, and two VMs with one
    // CID can't both start); the device is added when missing.
    public static string WithCid(string? args, uint cid) =>
        Cid(args) is null ? $"{args} -device vhost-vsock-pci,guest-cid={cid}".Trim()
            : Regex.Replace(args!, @"(vhost-vsock-pci\b[^ ]*\bguest-cid=)\d+", $"${{1}}{cid}");

    record struct PveVm(int Id, string Name, string Status, bool Template, string Pool);

    // Every VM on this node, with its pool (the cluster resource list is the API call that carries it).
    async Task<List<PveVm>> List(CancellationToken ct) =>
        (await api.Call("get", "/cluster/resources", new Dictionary<string, string> { ["type"] = "vm" }, ct))?.AsArray()
            .Where(v => v?["node"]?.GetValue<string>() == node && v["type"]?.GetValue<string>() == "qemu")
            .Select(v => new PveVm(v!["vmid"]!.GetValue<int>(), v["name"]?.GetValue<string>() ?? "", v["status"]?.GetValue<string>() ?? "",
                v["template"] is { } t && t.ToString() is "1" or "true", v["pool"]?.GetValue<string>() ?? ""))
            .ToList() ?? [];

    bool InRange(PveVm v) => options.Pool != "" && v.Pool == options.Pool;

    // The one range VM with this name. Proxmox allows two VMs with one name: then neither is meant, and a name that
    // also belongs to a VM outside the range is refused too.
    async Task<PveVm> Find(string name, CancellationToken ct)
    {
        var named = (await List(ct)).Where(v => v.Name == name).ToList();
        if (named.Count == 0) throw new KeyNotFoundException($"no VM named {name}");
        if (options.Pool == "") throw new UnauthorizedAccessException("Proxmox:Pool is not set: no VM is in the range");
        if (!named.All(InRange)) throw new UnauthorizedAccessException($"{name} is not a range VM (pool {options.Pool})");
        if (named.Count > 1) throw new InvalidOperationException($"more than one VM named {name}");
        return named[0];
    }

    // Input --------------------------------------------------------------

    (int W, int H) ScreenOf(VmHandle vm) => vm.Screen ?? options.Screen;

    // Guest pixel to the tablet's 0-32767 range, ends included.
    public static int Abs(int pixel, int size) => Math.Clamp((int)Math.Round(pixel * (double)AbsMax / Math.Max(1, size - 1)), 0, AbsMax);

    static Task Send(Vm vm, CancellationToken ct, params JsonObject[] events) =>
        vm.Qmp.Execute("input-send-event", Qmp.Events(events), ct);

    public async Task Move(VmHandle h, int x, int y, uint durationMs, CancellationToken ct)
    {
        var vm = (Vm)h;
        var (w, hh) = ScreenOf(vm);
        if (x < 0 || y < 0 || x >= w || y >= hh) throw new ArgumentException($"({x}, {y}) is off the {w}x{hh} screen");
        int steps = Human.Steps(vm.Pointer, durationMs), i = 0;
        foreach (var (px, py) in Human.Path(vm.Pointer, x, y, steps))
        {
            await Send(vm, ct, Qmp.Abs("x", Abs(px, w)), Qmp.Abs("y", Abs(py, hh)));
            if (++i < steps) await Task.Delay((int)(durationMs / steps), ct);
        }
        vm.Pointer = (x, y);
    }

    static string ButtonName(int button) => button switch
    {
        1 => "left",
        2 => "right",
        3 => "middle",
        _ => throw new ArgumentException("no such button"),
    };

    public async Task Click(VmHandle h, int button, uint count, CancellationToken ct)
    {
        var vm = (Vm)h;
        var name = ButtonName(button);
        for (uint i = 0; i < Math.Max(1, count); i++)
        {
            await Send(vm, ct, Qmp.Button(name, true));
            await Task.Delay(Random.Shared.Next(45, 95), ct);
            await Send(vm, ct, Qmp.Button(name, false));
            if (i + 1 < count) await Task.Delay(Random.Shared.Next(70, 130), ct);
        }
    }

    public void Key(VmHandle h, uint vk, bool down) => Wait(Send((Vm)h, CancellationToken.None, Qmp.Key(Keys.Qcode(vk), down)));

    public async Task Chord(VmHandle h, IReadOnlyList<uint> keys, CancellationToken ct)
    {
        var vm = (Vm)h;
        var codes = keys.Select(Keys.Qcode).ToList(); // an unknown key fails before anything is pressed
        foreach (var k in codes) { await Send(vm, ct, Qmp.Key(k, true)); await Task.Delay(Random.Shared.Next(20, 50), ct); }
        for (int i = codes.Count - 1; i >= 0; i--) { await Send(vm, ct, Qmp.Key(codes[i], false)); await Task.Delay(Random.Shared.Next(15, 35), ct); }
    }

    public async Task Type(VmHandle h, string text, uint meanMs, uint jitterMs, CancellationToken ct)
    {
        var vm = (Vm)h;
        var keys = text.Select(Keys.ForChar).ToList(); // nothing is typed when one character can't be
        foreach (var (q, shift) in keys)
        {
            if (shift) await Send(vm, ct, Qmp.Key("shift", true));
            await Send(vm, ct, Qmp.Key(q, true));
            await Task.Delay(Random.Shared.Next(25, 60), ct); // a key is held, not tapped in zero time
            await Send(vm, ct, Qmp.Key(q, false));
            if (shift) await Send(vm, ct, Qmp.Key("shift", false));
            if (Human.KeyDelay(meanMs, jitterMs) is var delay and > 0) await Task.Delay(delay, ct);
        }
    }

    public void CtrlAltDel(VmHandle h) => Wait(((Vm)h).Qmp.Execute("send-key", new JsonObject
    {
        ["keys"] = new JsonArray(new[] { "ctrl", "alt", "delete" }.Select(k => (JsonNode)new JsonObject { ["type"] = "qcode", ["data"] = k }).ToArray()),
    }, CancellationToken.None));

    public async Task Scroll(VmHandle h, int notches, CancellationToken ct)
    {
        var vm = (Vm)h;
        var wheel = notches > 0 ? "wheel-up" : "wheel-down";
        for (int i = 0; i < Math.Abs(notches); i++)
        {
            await Send(vm, ct, Qmp.Button(wheel, true), Qmp.Button(wheel, false));
            await Task.Delay(Random.Shared.Next(40, 110), ct);
        }
    }

    // Console frame ----------------------------------------------------------

    public (byte[] Png, int Width, int Height) Screenshot(VmHandle h) => Wait(Screendump(((Vm)h).Qmp, CancellationToken.None));

    async Task<(byte[] Png, int Width, int Height)> Screendump(Qmp qmp, CancellationToken ct)
    {
        if (OperatingSystem.IsWindows()) Directory.CreateDirectory(options.ScratchDir); // tests only
        else Directory.CreateDirectory(options.ScratchDir, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        var file = Path.Combine(options.ScratchDir, $"{Guid.NewGuid():N}.png");
        try
        {
            await qmp.Execute("screendump", new JsonObject { ["filename"] = file, ["format"] = "png" }, ct);
            var png = await File.ReadAllBytesAsync(file, ct);
            return (png, BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(16)), BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(20)));
        }
        finally { File.Delete(file); }
    }

    // The monitor's frames: from one persistent view-only VNC session per VM (Captures), not screendump, so watching
    // costs QEMU's main loop no PNG encode. Every frame still resolves the range VM; a different VM behind the name
    // (VMID or identity) starts a new session.
    readonly Captures captures = new();

    public byte[] Frame(string name, int maxWidth)
    {
        var vm = Get(name);
        return captures.Frame(name, $"{vm.Id}|{vm.Identity}", vm.Qmp.VncClient, maxWidth);
    }

    public List<VmInfo> ListVms() =>
        Wait(List(CancellationToken.None)).Where(v => !v.Template && InRange(v))
            .Select(v => v.Status == "running"
                ? new VmInfo(v.Name, "running", options.Screen.W, options.Screen.H)
                : new VmInfo(v.Name, v.Status == "stopped" ? "off" : "other", 0, 0))
            .OrderBy(v => v.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();

    // Lifecycle (Proxmox API) ----------------------------------------------

    public async Task<string> Lifecycle(string name, Op op, string checkpoint, string target, CancellationToken ct)
    {
        if (!VmName.Valid(name) || (checkpoint != "" && !VmName.Valid(checkpoint)) || (target != "" && !VmName.Valid(target)))
            throw new ArgumentException("invalid name");
        if (op == Op.Clone && !target.StartsWith("ludius-"))
            throw new ArgumentException("clone needs a target named ludius-*");
        if (op is Op.Checkpoint or Op.Restore && checkpoint == "")
            throw new ArgumentException("checkpoint and restore need a checkpoint name");
        if (op == Op.Delete && (!name.StartsWith("ludius-") || target != name))
            throw new ArgumentException("delete is limited to ludius-* VMs and needs the VM name repeated as confirmation");
        var vm = await Find(name, ct); // fresh every call: range pool, name and VMID as they are now
        if (op != Op.State) { Forget(name); Invalidate(vm.Id); }
        var at = $"/nodes/{node}/qemu/{vm.Id}";
        switch (op)
        {
            case Op.State: break;
            case Op.Start:
                if (vm.Status != "running") await Capped(vm, () => Complete(api.Call("create", $"{at}/status/start", null, ct), ct), ct);
                break;
            case Op.Stop:
                await Complete(api.Call("create", $"{at}/status/shutdown", new Dictionary<string, string> { ["forceStop"] = "1", ["timeout"] = "120" }, ct), ct);
                break;
            case Op.Checkpoint:
                // Never under an existing name: a baseline is never replaced (as on Hyper-V).
                if ((await api.Call("get", $"{at}/snapshot", null, ct))?.AsArray().Any(s => s?["name"]?.GetValue<string>() == checkpoint) == true)
                    throw new InvalidOperationException($"checkpoint '{checkpoint}' exists on {name} and is never duplicated or replaced: use a new name");
                await Complete(api.Call("create", $"{at}/snapshot", new Dictionary<string, string>
                    { ["snapname"] = checkpoint, ["vmstate"] = vm.Status == "running" ? "1" : "0" }, ct), ct);
                break;
            case Op.Restore: // a snapshot with RAM comes back running, as a Hyper-V checkpoint of a running VM does
                await Capped(vm, () => Complete(api.Call("create", $"{at}/snapshot/{checkpoint}/rollback", null, ct), ct), ct);
                break;
            case Op.Clone:
                return await Clone(vm, checkpoint, target, ct);
            case Op.Delete:
                if (vm.Status == "running") await Complete(api.Call("create", $"{at}/status/stop", null, ct), ct);
                await Complete(api.Call("delete", at, new Dictionary<string, string> { ["purge"] = "1", ["destroy-unreferenced-disks"] = "1" }, ct), ct);
                Invalidate(vm.Id); // also refuses a lookup that started before the delete
                return "Deleted";
        }
        return await State(vm.Id, ct);
    }

    // The host's VM cap (Agent:MaxRunningVms): a start or restore is refused while that many other range VMs run.
    // Infrastructure (Proxmox:CapExempt, known range servers by VMID and name) neither counts nor is capped. One
    // activation at a time in this agent (one agent per node). Each VM's state is read from its own status (QMP),
    // not the periodically refreshed cluster resource list, so a VM started a moment ago counts at once.
    bool Exempt(PveVm v) => options.CapExempt.Contains((v.Id, v.Name));

    async Task Capped(PveVm vm, Func<Task> activate, CancellationToken ct)
    {
        await activation.WaitAsync(ct);
        try
        {
            if (!Exempt(vm))
            {
                var up = new List<string>();
                foreach (var v in (await List(ct)).Where(v => InRange(v) && !v.Template && v.Id != vm.Id && !Exempt(v)))
                {
                    var off = await State(v.Id, ct) == "Off"; // an unreadable state throws: refused, nothing started
                    if (!off) reserved.Remove(v.Id); // confirmed up: it counts by its state from now on
                    if (!off || (reserved.TryGetValue(v.Id, out var at) && Now() - at < options.ReservationTtl)) up.Add(v.Name);
                }
                if (up.Count >= options.MaxRunning)
                    throw new InvalidOperationException($"VM cap {options.MaxRunning} (Agent:MaxRunningVms): {string.Join(", ", up)} up; {vm.Name} not activated");
            }
            reserved[vm.Id] = Now();
            var confirmed = false;
            try
            {
                await activate();
                confirmed = true;
            }
            // Only a task Proxmox reports as finished and failed releases the slot. A cancellation or any other error (an
            // unreadable task status, a failed create call) may leave the VM coming up: the reservation stays until it is
            // seen running or ReservationTtl passes.
            catch (PveTaskFailed) { confirmed = true; throw; }
            finally { if (confirmed) reserved.Remove(vm.Id); }
        }
        finally { activation.Release(); }
    }

    // A clone's VMID: Proxmox's next free one, but never a VMID the cap exempts (a persona there would escape it).
    async Task<string> FreeId(CancellationToken ct)
    {
        var id = int.Parse((await api.Call("get", "/cluster/nextid", null, ct))!.ToString());
        var exempt = options.CapExempt.Select(e => e.Id).ToHashSet();
        while (exempt.Contains(id))
        {
            id = exempt.Where(e => e >= id).Max() + 1;
            // Proxmox refuses a taken VMID here (and answers it back when free).
            id = int.Parse((await api.Call("get", "/cluster/nextid", new Dictionary<string, string> { ["vmid"] = $"{id}" }, ct))!.ToString());
        }
        return $"{id}";
    }

    // A template clones linked (spec: linked clones), from its base; any other VM as a full copy of `checkpoint`.
    // The source is untouched. The clone gets its own vsock CID, its VM id.
    async Task<string> Clone(PveVm src, string checkpoint, string target, CancellationToken ct)
    {
        if (!src.Template && checkpoint == "") throw new ArgumentException("clone of a VM (not a template) needs a checkpoint");
        if ((await List(ct)).Any(v => v.Name == target)) throw new InvalidOperationException($"a VM named {target} exists");
        var newId = await FreeId(ct);
        var args = new Dictionary<string, string>
            { ["newid"] = newId, ["name"] = target, ["full"] = src.Template ? "0" : "1", ["pool"] = options.Pool };
        if (checkpoint != "") args["snapname"] = checkpoint;
        // Until its own vsock CID is set the clone carries its source's: no handle on it is cached or handed out (a
        // lookup refuses), and the VMID is invalidated again once configured, so a lookup that ran meanwhile is refused.
        // If setting the CID fails or is cancelled, the clone keeps its source's CID and every lookup refuses it (OwnCid).
        var id = int.Parse(newId);
        lock (vms) configuring[id] = configuring.GetValueOrDefault(id) + 1;
        try
        {
            Invalidate(id); // a handle left on a deleted VM with this VMID
            Forget(target);
            await Complete(api.Call("create", $"/nodes/{node}/qemu/{src.Id}/clone", args, ct), ct);
            var cfg = await api.Call("get", $"/nodes/{node}/qemu/{newId}/config", null, ct);
            await api.Call("set", $"/nodes/{node}/qemu/{newId}/config",
                new Dictionary<string, string> { ["args"] = WithCid(cfg?["args"]?.GetValue<string>(), (uint)id) }, ct);
            if (Cid((await api.Call("get", $"/nodes/{node}/qemu/{newId}/config", null, ct))?["args"]?.GetValue<string>()) != (uint)id)
                throw new IOException($"{target} (VM {id}) did not take its vsock CID: refused until it is set");
        }
        finally
        {
            lock (vms)
            {
                if (--configuring[id] == 0) configuring.Remove(id);
                Invalidate(id);
                Forget(target);
            }
        }
        return await State(id, ct);
    }

    async Task<string> State(int id, CancellationToken ct)
    {
        var s = await api.Call("get", $"/nodes/{node}/qemu/{id}/status/current", null, ct);
        return (s?["qmpstatus"]?.GetValue<string>() ?? s?["status"]?.GetValue<string>()) switch
        {
            "running" => "Running",
            "stopped" => "Off",
            "paused" or "suspended" => "Paused",
            var other => other ?? "Unknown",
        };
    }

    // A worker task (UPID) runs until its status is "stopped"; anything but exit status OK fails the call.
    async Task Complete(Task<JsonNode?> call, CancellationToken ct)
    {
        var upid = (await call)?.ToString();
        if (upid is null || !upid.StartsWith("UPID:")) return; // done synchronously
        while (true)
        {
            var s = await api.Call("get", $"/nodes/{node}/tasks/{upid}/status", null, ct);
            if (s?["status"]?.GetValue<string>() == "stopped")
            {
                var exit = s["exitstatus"]?.GetValue<string>();
                if (exit != "OK") throw new PveTaskFailed($"{upid}: {exit}");
                return;
            }
            await Task.Delay(options.PollMs, ct);
        }
    }
}

// A Proxmox task that finished with an error: its effect did not happen (unlike an unreadable task status).
sealed class PveTaskFailed(string message) : InvalidOperationException(message);

public sealed record ProxmoxOptions
{
    public string RunDir { get; init; } = "/var/run/qemu-server";
    public string ScratchDir { get; init; } = "/run/ludius-agent"; // screendumps, deleted after reading
    public uint VsockPort { get; init; } = 4555; // 0x11cb: the observer's Hyper-V service id is the vsock port template
    public (int W, int H) Screen { get; init; } = (1920, 1080); // fixed in VM config; the observer's report wins
    public int PollMs { get; init; } = 1000;
    public TimeSpan Recheck { get; init; } = TimeSpan.FromSeconds(5); // a cached VM handle's range check, at most this old
    public string Pool { get; init; } = ""; // the range's Proxmox pool; unset: every VM is refused
    public int MaxRunning { get; init; } = 1; // Agent:MaxRunningVms, the host's cap on running range VMs (owner's default)
    // Known range servers (VMID and name, e.g. 110 ludius-srv01): not counted, not capped. Both must match.
    public HashSet<(int Id, string Name)> CapExempt { get; init; } = [];
    public TimeSpan ReservationTtl { get; init; } = TimeSpan.FromMinutes(5); // a cancelled activation counts this long
}
