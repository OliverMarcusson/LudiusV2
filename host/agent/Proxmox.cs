using System.Buffers.Binary;
using System.Globalization;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Op = Ludius.V1.LifecycleRequest.Types.Op;

namespace Ludius.HostAgent;

sealed partial class Proxmox(IPveApi api, string node, ProxmoxOptions options) : IHypervisor
{
    const int AbsMax = 0x7FFF;

    public sealed class Vm : VmHandle
    {
        public required int Id;
        public required Qmp Qmp;
        public required string Identity;
        public DateTime CheckedAt;
        public Task? Checking;
    }

    readonly Dictionary<string, Vm> vms = [];
    readonly SemaphoreSlim activation = new(1, 1);
    readonly Dictionary<int, DateTime> reserved = [];

    long clock;
    readonly Dictionary<int, long> invalidated = [];
    readonly Dictionary<int, int> configuring = [];

    bool ChangedSince(int id, long seen) => configuring.ContainsKey(id) || invalidated.GetValueOrDefault(id) > seen;

    static IOException Raced(string name, int id) =>
        new($"{name} (VM {id}) was deleted, cloned or reconfigured during the lookup: try again");

    static uint? OwnCid(string name, int id, JsonNode? cfg)
    {
        var cid = Cid(cfg?["args"]?.GetValue<string>());
        return cid is null || cid == (uint)id ? cid
            : throw new IOException($"{name} (VM {id}) has vsock CID {cid}, not its VMID: refused until its CID is set");
    }

    public Func<uint, CancellationToken, Task<Socket>> ConnectGuest = (cid, ct) => Vsock.Connect(cid, options.VsockPort, ct);
    public Func<DateTime> Now = () => DateTime.UtcNow;

    public string Backend => "proxmox";
    VmHandle IHypervisor.Get(string name) => Get(name);

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
            TaskCompletionSource? mine = null;
            Task check;
            lock (vms)
            {
                if (Now() - cached.CheckedAt < options.Recheck && vms.TryGetValue(name, out var cur) && cur == cached) return cached;
                if (cached.Checking is null) cached.Checking = (mine = new(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
                check = cached.Checking;
            }
            if (mine is not null)
            {
                try { Recheck(cached); mine.SetResult(); }
                catch (Exception e) { mine.SetException(e); }
                finally { lock (vms) cached.Checking = null; }
            }
            Wait(check);
            lock (vms)
                if (!ChangedSince(cached.Id, seen) && vms.TryGetValue(name, out var still) && still == cached) return cached;
            Forget(name);
            throw Raced(name, cached.Id);
        }
        TaskCompletionSource<Vm>? first = null;
        Task<Vm>? resolve;
        lock (vms)
            if (!resolving.TryGetValue(name, out resolve))
                resolving[name] = resolve = (first = new(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
        if (first is not null)
        {
            try { first.SetResult(Resolve(name, seen)); }
            catch (Exception e) { first.SetException(e); }
            finally { lock (vms) resolving.Remove(name); }
        }
        return Wait(resolve);
    }

    readonly Dictionary<string, Task<Vm>> resolving = [];

    Vm Resolve(string name, long seen)
    {
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
            JsonNode? cfg = Conf(vm.Id, out var pending);
            if (cfg is not null && !pending)
            {
                if (!Local(VmList(), vm.Id)) throw new KeyNotFoundException($"{vm.Name} (VM {vm.Id}) is gone");
                if (cfg["name"]?.GetValue<string>() != vm.Name || !PoolMembers().Contains(vm.Id))
                    throw new UnauthorizedAccessException($"VM {vm.Id} is no longer the range VM {vm.Name} (pool {options.Pool})");
            }
            else
            {
                var now = Wait(List(CancellationToken.None)).Where(v => v.Id == vm.Id).ToList();
                if (now is not [var v]) throw new KeyNotFoundException($"{vm.Name} (VM {vm.Id}) is gone");
                if (v.Name != vm.Name || !InRange(v))
                    throw new UnauthorizedAccessException($"VM {vm.Id} is no longer the range VM {vm.Name} (pool {options.Pool})");
                cfg = Wait(api.Call("get", $"/nodes/{node}/qemu/{vm.Id}/config", null, CancellationToken.None));
            }
            if (IdentityOf(cfg) != vm.Identity)
                throw new UnauthorizedAccessException($"VM {vm.Id} named {vm.Name} is a different VM than before (VMID reused)");
            OwnCid(vm.Name, vm.Id, cfg);
            vm.CheckedAt = Now();
        }
        catch
        {
            Forget(vm.Name);
            throw;
        }
    }

    public static string IdentityOf(JsonNode? cfg) =>
        $"{cfg?["meta"]?.GetValue<string>()}|{(cfg?["smbios1"]?.GetValue<string>() is { } s ? SmbiosUuid().Match(s).Value : "")}";

    [GeneratedRegex(@"(?<=\buuid=)[0-9A-Fa-f-]+")]
    private static partial Regex SmbiosUuid();

    public void Forget(string name)
    {
        lock (vms) if (vms.Remove(name, out var vm)) vm.Channel.Dispose();
    }

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

    public static string WithCid(string? args, uint cid) =>
        Cid(args) is null ? $"{args} -device vhost-vsock-pci,guest-cid={cid}".Trim()
            : Regex.Replace(args!, @"(vhost-vsock-pci\b[^ ]*\bguest-cid=)\d+", $"${{1}}{cid}");

    record struct PveVm(int Id, string Name, string Status, bool Template, string Pool);

    async Task<List<PveVm>> List(CancellationToken ct) =>
        (await api.Call("get", "/cluster/resources", new Dictionary<string, string> { ["type"] = "vm" }, ct))?.AsArray()
            .Where(v => v?["node"]?.GetValue<string>() == node && v["type"]?.GetValue<string>() == "qemu")
            .Select(v => new PveVm(v!["vmid"]!.GetValue<int>(), v["name"]?.GetValue<string>() ?? "", v["status"]?.GetValue<string>() ?? "",
                v["template"] is { } t && t.ToString() is "1" or "true", v["pool"]?.GetValue<string>() ?? ""))
            .ToList() ?? [];

    bool InRange(PveVm v) => options.Pool != "" && v.Pool == options.Pool;

    JsonObject VmList() =>
        JsonNode.Parse(api.Read(".vmlist") ?? throw new IOException("no /etc/pve/.vmlist"))?["ids"] as JsonObject
            ?? throw new IOException("/etc/pve/.vmlist has no ids");

    bool Local(JsonObject ids, int id) =>
        ids[$"{id}"] is JsonObject e && e["node"]?.GetValue<string>() == node && e["type"]?.GetValue<string>() == "qemu";

    HashSet<int> PoolMembers()
    {
        if (options.Pool == "") return [];
        var pools = (api.Read("user.cfg") ?? throw new IOException("no /etc/pve/user.cfg")).Split('\n')
            .Select(l => l.Trim())
            .Where(l => l.StartsWith("pool", StringComparison.OrdinalIgnoreCase)
                && (l.StartsWith("pool:") ? true : throw new IOException("/etc/pve/user.cfg: a pool line is malformed")))
            .Select(l => l.Split(':') is { Length: >= 5 } f ? (Pool: f[1], Ids: f[3].Split(',', StringSplitOptions.RemoveEmptyEntries)
                    .Select(v => int.Parse(v, NumberStyles.None, CultureInfo.InvariantCulture)).ToList())
                : throw new IOException("/etc/pve/user.cfg: a pool line is malformed"))
            .ToList();
        var ours = pools.Where(p => p.Pool == options.Pool).ToList();
        if (ours is []) return [];
        if (ours.Count > 1) throw new IOException($"/etc/pve/user.cfg: pool {options.Pool} is defined more than once");
        var members = ours[0].Ids.ToHashSet();
        if (pools.SelectMany(p => p.Ids).Where(members.Contains).Count() != members.Count || members.Count != ours[0].Ids.Count)
            throw new IOException($"/etc/pve/user.cfg: a VM of pool {options.Pool} is listed twice or also in another pool");
        return members;
    }

    JsonObject? Conf(int id, out bool pending)
    {
        pending = false;
        if (api.Read($"nodes/{node}/qemu-server/{id}.conf") is not { } text) return null;
        var cfg = new JsonObject();
        var current = true;
        foreach (var line in text.Split('\n'))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            if (line.StartsWith('['))
            {
                if (PendingHeader().IsMatch(line)) pending = true;
                else if (!SectionHeader().IsMatch(line)) throw new IOException($"VM {id} config: unexpected section header");
                current = false;
                continue;
            }
            if (!current || line.StartsWith('#')) continue;
            if (ConfLine().Match(line) is not { Success: true } m || !cfg.TryAdd(m.Groups[1].Value, m.Groups[2].Value))
                throw new IOException($"VM {id} config: unexpected line");
        }
        return cfg;
    }

    [GeneratedRegex(@"^\[PENDING\]\s*$", RegexOptions.IgnoreCase)]
    private static partial Regex PendingHeader();

    [GeneratedRegex(@"^\[([a-z][a-z0-9_\-]+|special:(cloudinit|fleecing))\]\s*$", RegexOptions.IgnoreCase)]
    private static partial Regex SectionHeader();

    [GeneratedRegex(@"^([a-z][a-z_\-]*\d*):\s*(.+?)\s*$")]
    private static partial Regex ConfLine();

    async Task<PveVm> Find(string name, CancellationToken ct)
    {
        var named = (await List(ct)).Where(v => v.Name == name).ToList();
        if (named.Count == 0) throw new KeyNotFoundException($"no VM named {name}");
        if (options.Pool == "") throw new UnauthorizedAccessException("Proxmox:Pool is not set: no VM is in the range");
        if (!named.All(InRange)) throw new UnauthorizedAccessException($"{name} is not a range VM (pool {options.Pool})");
        if (named.Count > 1) throw new InvalidOperationException($"more than one VM named {name}");
        return named[0];
    }


    (int W, int H) ScreenOf(VmHandle vm) => vm.Screen ?? options.Screen;

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
        var codes = keys.Select(Keys.Qcode).ToList();
        foreach (var k in codes) { await Send(vm, ct, Qmp.Key(k, true)); await Task.Delay(Random.Shared.Next(20, 50), ct); }
        for (int i = codes.Count - 1; i >= 0; i--) { await Send(vm, ct, Qmp.Key(codes[i], false)); await Task.Delay(Random.Shared.Next(15, 35), ct); }
    }

    public async Task Type(VmHandle h, string text, uint meanMs, uint jitterMs, CancellationToken ct)
    {
        var vm = (Vm)h;
        var keys = text.Select(Keys.ForChar).ToList();
        foreach (var (q, shift) in keys)
        {
            if (shift) await Send(vm, ct, Qmp.Key("shift", true));
            await Send(vm, ct, Qmp.Key(q, true));
            await Task.Delay(Random.Shared.Next(25, 60), ct);
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


    public (byte[] Png, int Width, int Height) Screenshot(VmHandle h) => Wait(Screendump(((Vm)h).Qmp, CancellationToken.None));

    async Task<(byte[] Png, int Width, int Height)> Screendump(Qmp qmp, CancellationToken ct)
    {
        if (OperatingSystem.IsWindows()) Directory.CreateDirectory(options.ScratchDir);
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

    readonly Captures captures = new();

    public byte[] Frame(string name, int maxWidth)
    {
        Vm vm;
        try
        {
            vm = Get(name);
            if (options.CapExempt.Contains((vm.Id, vm.Name))) throw new InvalidOperationException($"{name} is a range server: not on the monitor");
        }
        catch
        {
            captures.Close(name);
            throw;
        }
        return captures.Frame(name, $"{vm.Id}|{vm.Identity}", vm.Qmp.VncClient, maxWidth);
    }

    public List<VmInfo> ListVms()
    {
        var ids = VmList();
        var list = new List<VmInfo>();
        foreach (var id in PoolMembers().Where(id => Local(ids, id)))
        {
            if (Conf(id, out _) is not { } cfg || cfg["template"]?.GetValue<string>() == "1") continue;
            var name = cfg["name"]?.GetValue<string>() ?? "";
            if (options.CapExempt.Contains((id, name))) continue;
            list.Add(PidStatus(id) is var s && s == "running"
                ? new VmInfo(name, s, options.Screen.W, options.Screen.H)
                : new VmInfo(name, s, 0, 0));
        }
        return list.OrderBy(v => v.Name, StringComparer.OrdinalIgnoreCase).ToList();
    }

    string PidStatus(int id)
    {
        string text;
        try { text = File.ReadAllText(Path.Combine(options.RunDir, $"{id}.pid")); }
        catch (FileNotFoundException) { return "off"; }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return "other"; }
        if (!int.TryParse(text.Trim(), out var pid)) return "other";
        try
        {
            using var p = System.Diagnostics.Process.GetProcessById(pid);
            return p.HasExited ? "off" : "running";
        }
        catch (ArgumentException) { return "off"; }
    }


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
        var vm = await Find(name, ct);
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
                if ((await api.Call("get", $"{at}/snapshot", null, ct))?.AsArray().Any(s => s?["name"]?.GetValue<string>() == checkpoint) == true)
                    throw new InvalidOperationException($"checkpoint '{checkpoint}' exists on {name} and is never duplicated or replaced: use a new name");
                await Complete(api.Call("create", $"{at}/snapshot", new Dictionary<string, string>
                    { ["snapname"] = checkpoint, ["vmstate"] = vm.Status == "running" ? "1" : "0" }, ct), ct);
                break;
            case Op.Restore:
                await Capped(vm, () => Complete(api.Call("create", $"{at}/snapshot/{checkpoint}/rollback", null, ct), ct), ct);
                break;
            case Op.Clone:
                return await Clone(vm, checkpoint, target, ct);
            case Op.Delete:
                if (vm.Status == "running") await Complete(api.Call("create", $"{at}/status/stop", null, ct), ct);
                await Complete(api.Call("delete", at, new Dictionary<string, string> { ["purge"] = "1", ["destroy-unreferenced-disks"] = "1" }, ct), ct);
                Invalidate(vm.Id);
                return "Deleted";
        }
        return await State(vm.Id, ct);
    }

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
                    var off = await State(v.Id, ct) == "Off";
                    if (!off) reserved.Remove(v.Id);
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
            catch (PveTaskFailed) { confirmed = true; throw; }
            finally { if (confirmed) reserved.Remove(vm.Id); }
        }
        finally { activation.Release(); }
    }

    async Task<string> FreeId(CancellationToken ct)
    {
        var id = int.Parse((await api.Call("get", "/cluster/nextid", null, ct))!.ToString());
        var exempt = options.CapExempt.Select(e => e.Id).ToHashSet();
        while (exempt.Contains(id))
        {
            id = exempt.Where(e => e >= id).Max() + 1;
            id = int.Parse((await api.Call("get", "/cluster/nextid", new Dictionary<string, string> { ["vmid"] = $"{id}" }, ct))!.ToString());
        }
        return $"{id}";
    }

    async Task<string> Clone(PveVm src, string checkpoint, string target, CancellationToken ct)
    {
        if (!src.Template && checkpoint == "") throw new ArgumentException("clone of a VM (not a template) needs a checkpoint");
        if ((await List(ct)).Any(v => v.Name == target)) throw new InvalidOperationException($"a VM named {target} exists");
        var newId = await FreeId(ct);
        var args = new Dictionary<string, string>
            { ["newid"] = newId, ["name"] = target, ["full"] = src.Template ? "0" : "1", ["pool"] = options.Pool };
        if (checkpoint != "") args["snapname"] = checkpoint;
        var id = int.Parse(newId);
        lock (vms) configuring[id] = configuring.GetValueOrDefault(id) + 1;
        try
        {
            Invalidate(id);
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

    async Task Complete(Task<JsonNode?> call, CancellationToken ct)
    {
        var upid = (await call)?.ToString();
        if (upid is null || !upid.StartsWith("UPID:")) return;
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

sealed class PveTaskFailed(string message) : InvalidOperationException(message);

public sealed record ProxmoxOptions
{
    public string RunDir { get; init; } = "/var/run/qemu-server";
    public string ScratchDir { get; init; } = "/run/ludius-agent";
    public uint VsockPort { get; init; } = 4555;
    public (int W, int H) Screen { get; init; } = (1920, 1080);
    public int PollMs { get; init; } = 1000;
    public TimeSpan Recheck { get; init; } = TimeSpan.FromSeconds(5);
    public string Pool { get; init; } = "";
    public int MaxRunning { get; init; } = 1;
    public HashSet<(int Id, string Name)> CapExempt { get; init; } = [];
    public TimeSpan ReservationTtl { get; init; } = TimeSpan.FromMinutes(5);
}
