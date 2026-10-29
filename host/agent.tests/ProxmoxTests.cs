using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Google.Protobuf;
using Ludius.HostAgent;
using Ludius.Observer;
using Ludius.V1;
using Xunit;
using Op = Ludius.V1.LifecycleRequest.Types.Op;

namespace Ludius.HostAgent.Tests;

// A QEMU QMP socket: greets, accepts qmp_capabilities, records every command and answers {"return": {}}, with an
// asynchronous event before each answer (QEMU may interleave them). Unix sockets work on Windows 10+ as on Linux.
sealed class FakeQmp : IDisposable
{
    public readonly string Dir = Directory.CreateTempSubdirectory("ludius-qmp").FullName;
    public readonly List<JsonObject> Commands = [];
    public int Connections;
    public Func<JsonObject, JsonObject>? Answer; // default: {"return": {}}
    readonly Socket listener = new(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);

    public FakeQmp(int vmid)
    {
        listener.Bind(new UnixDomainSocketEndPoint(Path.Combine(Dir, $"{vmid}.qmp")));
        listener.Listen(4);
        File.WriteAllText(Path.Combine(Dir, $"{vmid}.pid"), $"{Environment.ProcessId}\n"); // its QEMU runs
        _ = Task.Run(Serve);
    }

    async Task Serve()
    {
        while (true)
        {
            Socket c;
            try { c = await listener.AcceptAsync(); } catch { return; }
            Interlocked.Increment(ref Connections);
            using var ns = new NetworkStream(c, ownsSocket: true);
            using var r = new StreamReader(ns);
            async Task Say(string s) => await ns.WriteAsync(Encoding.UTF8.GetBytes(s + "\r\n"));
            await Say("""{"QMP": {"version": {"qemu": {"major": 9, "minor": 0, "micro": 2}}, "capabilities": []}}""");
            while (await r.ReadLineAsync() is { } line)
            {
                var o = JsonNode.Parse(line)!.AsObject();
                if ((string?)o["execute"] != "qmp_capabilities") lock (Commands) Commands.Add(o);
                await Say("""{"event": "RTC_CHANGE", "data": {"offset": 0}, "timestamp": {"seconds": 1, "microseconds": 0}}""");
                var answer = (string?)o["execute"] == "qmp_capabilities" ? null : Answer?.Invoke(o);
                await Say((answer ?? new JsonObject { ["return"] = new JsonObject() }).ToJsonString());
            }
        }
    }

    public List<JsonObject> Events() =>
        Commands.Where(c => (string?)c["execute"] == "input-send-event").SelectMany(c => c["arguments"]!["events"]!.AsArray().Select(e => e!.AsObject())).ToList();

    public void Dispose()
    {
        listener.Dispose();
        try { Directory.Delete(Dir, true); } catch (IOException) { }
    }
}

// The Proxmox API: a VM list, configs, snapshots and tasks, recording every call.
sealed class FakePve : IPveApi
{
    public readonly List<(string Method, string Path, IReadOnlyDictionary<string, string>? Args)> Calls = [];
    // Range VMs are in pool "ludius"; 200 and 201 are not range VMs, 300 runs on another node.
    public JsonArray Vms = JsonNode.Parse("""
        [{"vmid": 101, "name": "ludius-win10-01", "status": "running", "node": "pve1", "type": "qemu", "pool": "ludius"},
         {"vmid": 9000, "name": "ludius-golden", "status": "stopped", "template": 1, "node": "pve1", "type": "qemu", "pool": "ludius"},
         {"vmid": 102, "name": "ludius-dup", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"},
         {"vmid": 103, "name": "ludius-dup", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"},
         {"vmid": 200, "name": "web-prod", "status": "running", "node": "pve1", "type": "qemu"},
         {"vmid": 201, "name": "ludius-stray", "status": "running", "node": "pve1", "type": "qemu", "pool": "other"},
         {"vmid": 202, "name": "ludius-shadow", "status": "stopped", "template": 1, "node": "pve1", "type": "qemu"},
         {"vmid": 300, "name": "ludius-elsewhere", "status": "running", "node": "pve2", "type": "qemu", "pool": "ludius"}]
        """)!.AsArray();
    public Dictionary<int, string> Args = new() { [101] = "-device vhost-vsock-pci,guest-cid=101", [9000] = "-device vhost-vsock-pci,guest-cid=9000" };
    public JsonArray Snapshots = JsonNode.Parse("""[{"name": "clean"}, {"name": "current"}]""")!.AsArray();
    public string TaskExit = "OK";
    public string TaskStatus = "stopped"; // "running": a worker task that never finishes
    public string NextId = "104";
    public Dictionary<int, string> Status = []; // a VM's own (QMP) status; unset: its resource-list status
    public Dictionary<int, long> Ctime = []; // a VM created anew under a VMID gets a new ctime

    // Race tests hold a call open here (method, path) until they let it go.
    public Func<string, string, Task>? Gate;
    public bool IgnoreSet;
    public HashSet<int> Pending = []; // VMs whose config file carries a [PENDING] section
    public Dictionary<int, string> ConfTail = []; // raw lines right after a VM's current section
    public string UserCfgTail = ""; // raw lines appended to user.cfg
    public readonly List<string> Reads = [];

    // The pmxcfs files, rendered from the same state the API answers from.
    public string? Read(string file)
    {
        lock (Calls) Reads.Add(file);
        Gate?.Invoke("read", file).GetAwaiter().GetResult();
        lock (Calls)
        {
            int Id(JsonNode? v) => v!["vmid"]!.GetValue<int>();
            if (file == ".vmlist")
                return new JsonObject
                {
                    ["version"] = 1,
                    ["ids"] = new JsonObject(Vms.Select(v => KeyValuePair.Create($"{Id(v)}",
                        (JsonNode?)new JsonObject { ["node"] = v!["node"]!.GetValue<string>(), ["type"] = v["type"]!.GetValue<string>(), ["version"] = 1 }))),
                }.ToJsonString();
            if (file == "user.cfg")
                return "user:root@pam:1:0:::root@pve1::\n\n" + string.Concat(Vms.Where(v => v?["pool"] is not null).GroupBy(v => v!["pool"]!.GetValue<string>())
                    .Select(g => $"pool:{g.Key}:Range%3a pool:{string.Join(",", g.Select(Id))}::\n")) + UserCfgTail;
            if (System.Text.RegularExpressions.Regex.Match(file, @"^nodes/([^/]+)/qemu-server/(\d+)\.conf$") is not { Success: true } m) return null;
            var id = int.Parse(m.Groups[2].Value);
            if (Vms.FirstOrDefault(v => Id(v) == id && v!["node"]!.GetValue<string>() == m.Groups[1].Value) is not { } vm) return null;
            var conf = $"#a description\nboot: order=scsi0\nmeta: creation-qemu=9.0.2,ctime={Ctime.GetValueOrDefault(id, 1700000000)}\n"
                + $"name: {vm["name"]}\nsmbios1: uuid=0000{id:x4}-1111-2222-3333-444455556666\n"
                + (Args.GetValueOrDefault(id) is { } a ? $"args: {a}\n" : "") + (vm["template"] is not null ? "template: 1\n" : "");
            return conf + ConfTail.GetValueOrDefault(id, "") + (Pending.Contains(id) ? "\n[PENDING]\nmemory: 8192\n" : "") + "\n[clean]\nname: ludius-old\nsnaptime: 1700000001\n";
        }
    }

    public async Task<JsonNode?> Call(string method, string path, IReadOnlyDictionary<string, string>? args, CancellationToken ct)
    {
        lock (Calls) Calls.Add((method, path, args));
        if (Gate is { } gate) await gate(method, path);
        var parts = path.Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (!IgnoreSet && method == "set" && path.EndsWith("/config") && args?.GetValueOrDefault("args") is { } a) Args[int.Parse(parts[3])] = a;
        JsonNode? res = (method, path) switch
        {
            ("get", "/cluster/resources") when args?["type"] == "vm" => Vms.DeepClone(),
            ("get", "/cluster/nextid") => JsonValue.Create(args?.GetValueOrDefault("vmid") ?? NextId),
            ("get", _) when path.EndsWith("/config") => new JsonObject
            {
                ["args"] = Args.GetValueOrDefault(int.Parse(parts[3])),
                ["meta"] = $"creation-qemu=9.0.2,ctime={Ctime.GetValueOrDefault(int.Parse(parts[3]), 1700000000)}",
                ["smbios1"] = $"uuid=0000{int.Parse(parts[3]):x4}-1111-2222-3333-444455556666",
            },
            ("get", _) when path.EndsWith("/snapshot") => Snapshots.DeepClone(),
            ("get", _) when path.EndsWith("/status/current") => new JsonObject { ["status"] = Status.GetValueOrDefault(int.Parse(parts[3]),
                Vms.FirstOrDefault(v => v?["vmid"]?.GetValue<int>() == int.Parse(parts[3]))?["status"]?.GetValue<string>() ?? "running") },
            ("get", _) when path.Contains("/tasks/") => new JsonObject { ["status"] = TaskStatus, ["exitstatus"] = TaskExit },
            ("create" or "delete", _) => JsonValue.Create("UPID:pve1:0000ABCD:00000001:00000001:qmstart:101:root@pam:"),
            _ => null,
        };
        return res;
    }
}

public class ProxmoxTests
{
    static (Proxmox, FakePve, FakeQmp) Make()
    {
        var qmp = new FakeQmp(101);
        var pve = new FakePve();
        var px = new Proxmox(pve, "pve1", new ProxmoxOptions { RunDir = qmp.Dir, ScratchDir = qmp.Dir, PollMs = 1, Screen = (1920, 1080), Pool = "ludius" });
        return (px, pve, qmp);
    }

    [Fact]
    public void Pixels_map_onto_the_tablet_range_ends_included()
    {
        Assert.Equal(0, Proxmox.Abs(0, 1920));
        Assert.Equal(32767, Proxmox.Abs(1919, 1920));
        Assert.Equal(16375, Proxmox.Abs(959, 1920)); // 959 * 32767 / 1919, rounded
        Assert.Equal(32767, Proxmox.Abs(1079, 1080));
    }

    [Fact]
    public async Task Move_jumps_to_the_absolute_position_one_qmp_connection_per_step()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        await px.Move(vm, 1919, 0, 0, CancellationToken.None);
        var ev = qmp.Events();
        Assert.Equal(["abs", "abs"], ev.Select(e => (string)e["type"]!));
        Assert.Equal(32767, (int)ev[0]["data"]!["value"]!);
        Assert.Equal("y", (string)ev[1]["data"]!["axis"]!);
        Assert.Equal(0, (int)ev[1]["data"]!["value"]!);
        Assert.Equal(1, qmp.Connections); // never held open: Proxmox needs the socket too

        await px.Move(vm, 100, 100, 300, CancellationToken.None); // a path: many steps, ending on the target
        var last = qmp.Events().TakeLast(2).ToList();
        Assert.Equal(Proxmox.Abs(100, 1920), (int)last[0]["data"]!["value"]!);
        Assert.Equal(Proxmox.Abs(100, 1080), (int)last[1]["data"]!["value"]!);
        Assert.True(qmp.Connections > 3);
    }

    [Fact]
    public async Task The_observers_resolution_wins_over_the_configured_one()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        vm.Screen = (1280, 720);
        await px.Move(vm, 1279, 719, 0, CancellationToken.None);
        Assert.All(qmp.Events(), e => Assert.Equal(32767, (int)e["data"]!["value"]!));
        await Assert.ThrowsAsync<ArgumentException>(() => px.Move(vm, 1280, 10, 0, CancellationToken.None));
    }

    [Fact]
    public async Task Click_scroll_and_ctrl_alt_del()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        await px.Click(vm, 2, 2, CancellationToken.None);
        await px.Scroll(vm, -1, CancellationToken.None);
        px.CtrlAltDel(vm);
        var buttons = qmp.Events().Select(e => $"{e["data"]!["button"]}:{e["data"]!["down"]}").ToList();
        Assert.Equal(["right:true", "right:false", "right:true", "right:false", "wheel-down:true", "wheel-down:false"], buttons);
        var cad = qmp.Commands.Last();
        Assert.Equal("send-key", (string)cad["execute"]!);
        Assert.Equal(["ctrl", "alt", "delete"], cad["arguments"]!["keys"]!.AsArray().Select(k => (string)k!["data"]!));
    }

    [Fact]
    public async Task Typing_uses_us_layout_keys_with_shift_and_releases_everything()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        const string probe = "Ludius contract 0123 ~!@#$%^&*()_+{}|:<>?\n";
        await px.Type(vm, probe, 0, 0, CancellationToken.None);
        var keys = qmp.Events().Select(e => ((string)e["data"]!["key"]!["data"]!, (bool)e["data"]!["down"]!)).ToList();
        // Balanced: every key that went down came up.
        Assert.All(keys.GroupBy(k => k.Item1), g => Assert.Equal(g.Count(k => k.Item2), g.Count(k => !k.Item2)));
        Assert.Equal([("shift", true), ("l", true), ("l", false), ("shift", false), ("u", true)], keys.Take(5));
        Assert.Equal(("ret", true), keys[^2]);
        Assert.Contains(("bracket_left", true), keys); // '{'
        Assert.Contains(("backslash", true), keys); // '|'
        Assert.Equal(probe.Count(c => char.IsUpper(c) || "~!@#$%^&*()_+{}|:<>?".Contains(c)), keys.Count(k => k == ("shift", true)));
    }

    [Fact]
    public async Task Nothing_is_sent_for_text_or_keys_that_cannot_be_typed()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        await Assert.ThrowsAsync<ArgumentException>(() => px.Type(vm, "ok é", 0, 0, CancellationToken.None));
        await Assert.ThrowsAsync<ArgumentException>(() => px.Chord(vm, [0x11, 0xFF], CancellationToken.None));
        Assert.Empty(qmp.Commands);
    }

    [Fact]
    public async Task Chord_presses_in_order_and_releases_in_reverse()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        await px.Chord(px.Get("ludius-win10-01"), [0x5B, 0x52], CancellationToken.None); // Win+R
        Assert.Equal(["meta_l:true", "r:true", "r:false", "meta_l:false"],
            qmp.Events().Select(e => $"{e["data"]!["key"]!["data"]}:{e["data"]!["down"]}"));
    }

    [Fact]
    public async Task Qmp_errors_fail_the_call()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        qmp.Answer = _ => JsonNode.Parse("""{"error": {"class": "GenericError", "desc": "no tablet"}}""")!.AsObject();
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => px.Click(px.Get("ludius-win10-01"), 1, 1, CancellationToken.None));
        Assert.Contains("no tablet", e.Message);
    }

    [Fact]
    public void Screenshot_reads_the_screendump_and_deletes_it()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        // A PNG header with width 1920, height 1080 written where QEMU was told to.
        qmp.Answer = o =>
        {
            var png = new byte[33];
            BinaryPrimitives.WriteInt32BigEndian(png.AsSpan(16), 1920);
            BinaryPrimitives.WriteInt32BigEndian(png.AsSpan(20), 1080);
            File.WriteAllBytes((string)o["arguments"]!["filename"]!, png);
            return new JsonObject { ["return"] = new JsonObject() };
        };
        var (data, w, h) = px.Screenshot(px.Get("ludius-win10-01"));
        Assert.Equal((33, 1920, 1080), (data.Length, w, h));
        Assert.Equal("png", (string)qmp.Commands.Single()["arguments"]!["format"]!);
        Assert.Empty(Directory.GetFiles(qmp.Dir, "*.png"));
    }

    [Fact]
    public void Names_resolve_to_one_vm_and_its_vsock_cid()
    {
        var (px, _, qmp) = Make();
        using var _q = qmp;
        Assert.Equal(101, px.Get("ludius-win10-01").Id);
        Assert.Throws<KeyNotFoundException>(() => px.Get("ludius-nope"));
        Assert.Throws<InvalidOperationException>(() => px.Get("ludius-dup")); // two VMs, one name: neither is meant
        Assert.Throws<ArgumentException>(() => px.Get("bad;name"));
        Assert.Equal(7u, Proxmox.Cid("-cpu host -device vhost-vsock-pci,id=v0,guest-cid=7"));
        Assert.Null(Proxmox.Cid("-cpu host"));
        Assert.Equal("-cpu host -device vhost-vsock-pci,id=v0,guest-cid=104", Proxmox.WithCid("-cpu host -device vhost-vsock-pci,id=v0,guest-cid=9000", 104));
        Assert.Equal("-device vhost-vsock-pci,guest-cid=104", Proxmox.WithCid(null, 104));
    }

    [Fact]
    public async Task Lifecycle_start_stop_checkpoint_restore()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        Assert.Equal("Running", await px.Lifecycle("ludius-win10-01", Op.Start, "", "", CancellationToken.None));
        Assert.DoesNotContain(pve.Calls, c => c.Path.EndsWith("/status/start")); // already running: nothing to do
        await px.Lifecycle("ludius-win10-01", Op.Stop, "", "", CancellationToken.None);
        Assert.Contains(pve.Calls, c => c.Path == "/nodes/pve1/qemu/101/status/shutdown" && c.Args!["forceStop"] == "1");
        await px.Lifecycle("ludius-win10-01", Op.Checkpoint, "aged", "", CancellationToken.None);
        var snap = pve.Calls.Single(c => c.Method == "create" && c.Path.EndsWith("/snapshot"));
        Assert.Equal(("aged", "1"), (snap.Args!["snapname"], snap.Args["vmstate"])); // running: with memory, as Hyper-V
        await px.Lifecycle("ludius-win10-01", Op.Restore, "clean", "", CancellationToken.None);
        Assert.Contains(pve.Calls, c => c.Path == "/nodes/pve1/qemu/101/snapshot/clean/rollback");
        Assert.Contains(pve.Calls, c => c.Path.Contains("/tasks/UPID:")); // every worker task is waited for
    }

    [Fact]
    public async Task Checkpoints_are_never_replaced_and_failed_tasks_fail()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        await Assert.ThrowsAsync<InvalidOperationException>(() => px.Lifecycle("ludius-win10-01", Op.Checkpoint, "clean", "", CancellationToken.None));
        Assert.DoesNotContain(pve.Calls, c => c.Method == "create");
        await Assert.ThrowsAsync<ArgumentException>(() => px.Lifecycle("ludius-win10-01", Op.Restore, "", "", CancellationToken.None));
        pve.TaskExit = "snapshot 'nope' does not exist";
        var e = await Assert.ThrowsAsync<PveTaskFailed>(() => px.Lifecycle("ludius-win10-01", Op.Restore, "nope", "", CancellationToken.None));
        Assert.Contains("does not exist", e.Message);
    }

    static (FakePve, Func<int, Proxmox>) Capped(FakeQmp qmp)
    {
        var pve = new FakePve();
        foreach (var (id, name) in new[] { (105, "ludius-b"), (106, "ludius-c"), (110, "ludius-srv01"), (111, "ludius-impostor") })
            pve.Vms.Add(JsonNode.Parse($$"""{"vmid": {{id}}, "name": "{{name}}", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}"""));
        pve.Status[110] = "running";
        // 101 runs; srv01 (110) runs too and is exempt by VMID and name; 111 has an exempt VMID but another name.
        return (pve, max => new Proxmox(pve, "pve1", new ProxmoxOptions { RunDir = qmp.Dir, ScratchDir = qmp.Dir, PollMs = 1,
            Pool = "ludius", MaxRunning = max, CapExempt = [(110, "ludius-srv01"), (111, "ludius-ex01")] }));
    }

    [Fact]
    public async Task The_host_cap_counts_running_range_vms_except_infrastructure()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => px(1).Lifecycle("ludius-b", Op.Start, "", "", CancellationToken.None));
        Assert.Contains("ludius-win10-01", e.Message);
        Assert.DoesNotContain("ludius-srv01", e.Message); // other pools, nodes and the exempt server don't count
        await Assert.ThrowsAsync<InvalidOperationException>(() => px(1).Lifecycle("ludius-b", Op.Restore, "clean", "", CancellationToken.None));
        Assert.DoesNotContain(pve.Calls, c => c.Method == "create");
        await px(2).Lifecycle("ludius-b", Op.Start, "", "", CancellationToken.None);
        Assert.Contains(pve.Calls, c => c.Path == "/nodes/pve1/qemu/105/status/start");
        await px(9).Lifecycle("ludius-srv01", Op.Start, "", "", CancellationToken.None); // exempt: never capped
    }

    [Fact]
    public async Task The_cap_reads_each_vms_own_state_not_the_stale_resource_list()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        var agent = px(2);
        await agent.Lifecycle("ludius-b", Op.Start, "", "", CancellationToken.None);
        pve.Status[105] = "running"; // up now; /cluster/resources still says stopped until its next refresh
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => agent.Lifecycle("ludius-c", Op.Start, "", "", CancellationToken.None));
        Assert.Contains("ludius-b", e.Message);
    }

    [Fact]
    public async Task An_exempt_vmid_under_another_name_is_capped_and_counted()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        await Assert.ThrowsAsync<InvalidOperationException>(() => px(1).Lifecycle("ludius-impostor", Op.Start, "", "", CancellationToken.None));
        pve.Status[111] = "running";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => px(2).Lifecycle("ludius-b", Op.Start, "", "", CancellationToken.None));
        Assert.Contains("ludius-impostor", e.Message);
    }

    [Fact]
    public async Task A_cancelled_activation_still_counts_until_its_reservation_expires()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        var agent = px(2);
        var now = DateTime.UtcNow;
        agent.Now = () => now;
        pve.TaskStatus = "running"; // the start task never confirms
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => agent.Lifecycle("ludius-b", Op.Start, "", "", cts.Token));
        pve.TaskStatus = "stopped";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => agent.Lifecycle("ludius-c", Op.Start, "", "", CancellationToken.None));
        Assert.Contains("ludius-b", e.Message); // still stopped by its state, but its start may be under way
        pve.TaskExit = "start failed";
        now += TimeSpan.FromMinutes(6);
        await Assert.ThrowsAsync<PveTaskFailed>(() => agent.Lifecycle("ludius-c", Op.Start, "", "", CancellationToken.None));
        pve.TaskExit = "OK";
        await agent.Lifecycle("ludius-c", Op.Start, "", "", CancellationToken.None); // a failed start held no room
    }

    [Fact]
    public async Task An_unreadable_task_outcome_keeps_the_reservation()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        var agent = px(2);
        pve.Gate = (_, path) => path.Contains("/tasks/") ? throw new InvalidOperationException("pvesh get: timeout") : Task.CompletedTask;
        await Assert.ThrowsAsync<InvalidOperationException>(() => agent.Lifecycle("ludius-b", Op.Start, "", "", CancellationToken.None));
        pve.Gate = null;
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => agent.Lifecycle("ludius-c", Op.Start, "", "", CancellationToken.None));
        Assert.Contains("ludius-b", e.Message); // its start may still complete
    }

    [Fact]
    public async Task A_clone_never_takes_an_exempt_vmid()
    {
        using var qmp = new FakeQmp(101);
        var (pve, px) = Capped(qmp);
        pve.NextId = "110";
        await px(1).Lifecycle("ludius-golden", Op.Clone, "", "ludius-new", CancellationToken.None);
        Assert.Contains(pve.Calls, c => c.Path == "/cluster/nextid" && c.Args?["vmid"] == "112");
        Assert.Contains(pve.Calls, c => c.Path == "/nodes/pve1/qemu/9000/clone" && c.Args!["newid"] == "112");
    }

    [Fact]
    public async Task A_template_clones_linked_with_its_own_vsock_cid()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        pve.Args[104] = pve.Args[9000]; // the clone copies its source's args
        await px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        var clone = pve.Calls.Single(c => c.Path.EndsWith("/clone"));
        Assert.Equal("/nodes/pve1/qemu/9000/clone", clone.Path);
        Assert.Equal(("104", "ludius-win10-02", "0"), (clone.Args!["newid"], clone.Args["name"], clone.Args["full"]));
        Assert.False(clone.Args.ContainsKey("snapname"));
        var set = pve.Calls.Single(c => c.Method == "set");
        Assert.Equal(("/nodes/pve1/qemu/104/config", "-device vhost-vsock-pci,guest-cid=104"), (set.Path, set.Args!["args"]));

        // A VM (not a template) clones as a full copy of a named snapshot; targets are ludius-* and new.
        await px.Lifecycle("ludius-win10-01", Op.Clone, "clean", "ludius-win10-03", CancellationToken.None);
        var full = pve.Calls.Last(c => c.Path.EndsWith("/clone"));
        Assert.Equal(("1", "clean"), (full.Args!["full"], full.Args["snapname"]));
        await Assert.ThrowsAsync<ArgumentException>(() => px.Lifecycle("ludius-win10-01", Op.Clone, "", "ludius-x", CancellationToken.None));
        await Assert.ThrowsAsync<ArgumentException>(() => px.Lifecycle("ludius-golden", Op.Clone, "", "other", CancellationToken.None));
        await Assert.ThrowsAsync<InvalidOperationException>(() => px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-01", CancellationToken.None));
    }

    [Fact]
    public async Task Delete_needs_the_name_repeated_and_purges()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        await Assert.ThrowsAsync<ArgumentException>(() => px.Lifecycle("ludius-win10-01", Op.Delete, "", "", CancellationToken.None));
        Assert.Empty(pve.Calls);
        Assert.Equal("Deleted", await px.Lifecycle("ludius-win10-01", Op.Delete, "", "ludius-win10-01", CancellationToken.None));
        Assert.Equal(["/nodes/pve1/qemu/101/status/stop", "/nodes/pve1/qemu/101"],
            pve.Calls.Where(c => c.Method is "create" or "delete").Select(c => c.Path));
    }

    [Fact]
    public void Monitor_lists_vms_but_not_templates()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        var vms = px.ListVms();
        Assert.DoesNotContain(vms, v => v.Name == "ludius-golden");
        Assert.Contains(new VmInfo("ludius-win10-01", "running", 1920, 1080), vms);
        Assert.DoesNotContain(vms, v => v.Name is "web-prod" or "ludius-stray" or "ludius-elsewhere"); // not the range
        Assert.Contains(new VmInfo("ludius-dup", "off", 0, 0), vms); // no QEMU pid
        Assert.Empty(pve.Calls); // read from pmxcfs, no pvesh per poll
    }

    [Fact]
    public void Range_servers_are_not_on_the_monitor()
    {
        using var qmp = new FakeQmp(101);
        var px = new Proxmox(new FakePve(), "pve1", new ProxmoxOptions
            { RunDir = qmp.Dir, ScratchDir = qmp.Dir, Pool = "ludius", CapExempt = [(101, "ludius-win10-01")] });
        Assert.DoesNotContain(px.ListVms(), v => v.Name == "ludius-win10-01");
        var e = Assert.Throws<InvalidOperationException>(() => px.Frame("ludius-win10-01", 640));
        Assert.Contains("range server", e.Message);
        Assert.Equal(0, qmp.Connections); // no capture opened
    }

    public static TheoryData<Op, string, string> EveryOp() => new()
    {
        { Op.State, "", "" }, { Op.Start, "", "" }, { Op.Stop, "", "" }, { Op.Checkpoint, "x", "" },
        { Op.Restore, "clean", "" }, { Op.Clone, "clean", "ludius-new" },
    };

    // VMs outside the range pool (no pool, another pool, a ludius-* name or template outside the pool) are refused
    // for every operation, as clone sources too, and nothing reaches the API but the VM list.
    [Theory]
    [MemberData(nameof(EveryOp))]
    public async Task Vms_outside_the_range_pool_are_refused(Op op, string checkpoint, string target)
    {
        foreach (var name in new[] { "web-prod", "ludius-stray", "ludius-shadow" })
        {
            var (px, pve, qmp) = Make();
            using var _q = qmp;
            await Assert.ThrowsAsync<UnauthorizedAccessException>(() => px.Lifecycle(name, op, checkpoint, target, CancellationToken.None));
            Assert.All(pve.Calls, c => Assert.Equal(("get", "/cluster/resources"), (c.Method, c.Path)));
        }
    }

    [Fact]
    public async Task Delete_input_and_unset_pool_are_refused_outside_the_range()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        await Assert.ThrowsAsync<UnauthorizedAccessException>(() => px.Lifecycle("ludius-stray", Op.Delete, "", "ludius-stray", CancellationToken.None));
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("web-prod")); // input, observation, screenshots
        Assert.Throws<KeyNotFoundException>(() => px.Get("ludius-elsewhere")); // another node's VM is not this agent's
        Assert.DoesNotContain(pve.Calls, c => c.Method != "get" || c.Path != "/cluster/resources");
        Assert.Empty(qmp.Commands);

        var none = new Proxmox(pve, "pve1", new ProxmoxOptions { RunDir = qmp.Dir });
        await Assert.ThrowsAsync<UnauthorizedAccessException>(() => none.Lifecycle("ludius-win10-01", Op.Start, "", "", CancellationToken.None));
        Assert.Empty(none.ListVms());
    }

    [Fact]
    public async Task Clones_go_into_the_range_pool()
    {
        var (px, pve, qmp) = Make();
        using var _q = qmp;
        await px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        Assert.Equal("ludius", pve.Calls.Single(c => c.Path.EndsWith("/clone")).Args!["pool"]);
    }

    [Fact]
    public void Pvesh_passes_every_value_as_its_own_argument_without_a_shell()
    {
        var hostile = "a b \"c\" 'd' $(touch pwned); `id` | e";
        var psi = Pvesh.StartInfo("pvesh", "set", "/nodes/pve1/qemu/104/config", new Dictionary<string, string> { ["args"] = hostile });
        Assert.False(psi.UseShellExecute);
        Assert.Equal("pvesh", psi.FileName);
        Assert.Equal(["set", "/nodes/pve1/qemu/104/config", "--output-format", "json", "--args", hostile], psi.ArgumentList);
    }

    // A fake pvesh: records its argv one per line and prints a UPID as pvesh's JSON output would.
    [Fact]
    public async Task Pvesh_runs_the_process_and_values_arrive_verbatim()
    {
        if (OperatingSystem.IsWindows()) return; // a .cmd fake would go through cmd.exe: run in the Linux container
        var dir = Directory.CreateTempSubdirectory("ludius-pvesh").FullName;
        try
        {
            var exe = Path.Combine(dir, "pvesh");
            File.WriteAllText(exe, $"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > {dir}/argv\necho '\"UPID:pve1:1:2:3:qmstart:104:root@pam:\"'\n");
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            var hostile = $"a b \"c\" 'd' $(touch {dir}/pwned); `touch {dir}/pwned2`";
            var res = await new Pvesh(exe).Call("create", "/nodes/pve1/qemu/104/status/start", new Dictionary<string, string> { ["k"] = hostile }, CancellationToken.None);
            Assert.Equal("UPID:pve1:1:2:3:qmstart:104:root@pam:", res!.ToString());
            Assert.Equal(["create", "/nodes/pve1/qemu/104/status/start", "--output-format", "json", "--k", hostile],
                File.ReadAllLines(Path.Combine(dir, "argv")));
            Assert.False(File.Exists(Path.Combine(dir, "pwned")) || File.Exists(Path.Combine(dir, "pwned2")));
        }
        finally { Directory.Delete(dir, true); }
    }
}

// The guest channel over a connected socket (vsock on Proxmox): hello first, answers matched by id, reconnect after
// a drop. A TCP loopback stands in for vsock, since only the connect differs.
public class GuestChannelTests
{
    static void Send(Stream s, IMessage m)
    {
        var body = m.ToByteArray();
        var len = new byte[4];
        BinaryPrimitives.WriteUInt32LittleEndian(len, (uint)body.Length);
        s.Write(len);
        s.Write(body);
    }

    static ObserverRequest Read(Stream s)
    {
        var len = new byte[4];
        s.ReadExactly(len);
        var body = new byte[BinaryPrimitives.ReadUInt32LittleEndian(len)];
        s.ReadExactly(body);
        return ObserverRequest.Parser.ParseFrom(body);
    }

    [Fact]
    public async Task Requests_get_their_own_answer_and_survive_an_observer_restart()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        int accepted = 0;
        _ = Task.Run(() =>
        {
            while (true)
            {
                using var c = listener.AcceptTcpClient();
                var n = ++accepted;
                var s = c.GetStream();
                Send(s, new ObserverResponse { Hello = new Hello { Version = $"v{n}", User = "aberg" } });
                var req = Read(s);
                Send(s, new ObserverResponse { Id = 999, Error = "stale" }); // an answer to an older request
                Send(s, new ObserverResponse { Id = req.Id, File = new FileContent { Exists = false } });
                if (n == 1) continue; // first observer "restarts": drops the link after one answer
                Read(s);
            }
        });
        using var ch = new GuestChannel(async ct =>
        {
            var s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
            await s.ConnectAsync(IPAddress.Loopback, port, ct);
            return s;
        });
        var req = new ObserverRequest { ReadFile = new ReadFile { Path = "x" } };
        var a = await ch.Request(req, TimeSpan.FromSeconds(5), CancellationToken.None);
        Assert.Equal(ObserverResponse.BodyOneofCase.File, a.BodyCase);
        Assert.Equal("v1", ch.Hello!.Version);
        var b = await ch.Request(req, TimeSpan.FromSeconds(5), CancellationToken.None); // reconnects once
        Assert.Equal(ObserverResponse.BodyOneofCase.File, b.BodyCase);
        Assert.Equal("v2", ch.Hello!.Version);
    }

    [Fact]
    public async Task An_unreachable_observer_is_an_io_error()
    {
        using var ch = new GuestChannel(_ => throw new IOException("vsock connect to cid 101 port 4555: errno 19"));
        await Assert.ThrowsAsync<IOException>(() => ch.Request(new ObserverRequest { Ping = new Ping() }, TimeSpan.FromSeconds(1), CancellationToken.None));
    }

    [Fact]
    public async Task Vsock_connect_fails_as_io_not_as_an_unsupported_family()
    {
        if (!OperatingSystem.IsLinux()) return; // libc vsock: run in the Linux container (see docs/proxmox-bringup.md)
        await Assert.ThrowsAsync<IOException>(() => Vsock.Connect(4_000_000, 4555, CancellationToken.None));
    }
}

