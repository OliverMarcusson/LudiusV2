using System.Text.Json.Nodes;
using Ludius.HostAgent;
using Ludius.V1;
using Xunit;
using Op = Ludius.V1.LifecycleRequest.Types.Op;

namespace Ludius.HostAgent.Tests;

public class HandleRecheckTests
{
    sealed class Clock
    {
        public DateTime Now = new(2026, 10, 28, 12, 0, 0, DateTimeKind.Utc);
    }

    static (Proxmox, FakePve, FakeQmp, Clock) Make()
    {
        var qmp = new FakeQmp(101);
        var pve = new FakePve();
        var clock = new Clock();
        var px = new Proxmox(pve, "pve1", new ProxmoxOptions { RunDir = qmp.Dir, ScratchDir = qmp.Dir, PollMs = 1, Pool = "ludius" })
            { Now = () => clock.Now };
        return (px, pve, qmp, clock);
    }

    static JsonObject Entry(FakePve pve, int vmid) => pve.Vms.Single(v => (int)v!["vmid"]! == vmid)!.AsObject();

    static int Lookups(FakePve pve) => pve.Calls.Count(c => c.Path == "/cluster/resources");

    [Fact]
    public void Removed_from_the_pool_is_refused_once_the_check_is_stale()
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        Entry(pve, 101).Remove("pool");

        clock.Now += TimeSpan.FromSeconds(4);
        var lookups = Lookups(pve);
        Assert.Same(vm, px.Get("ludius-win10-01"));
        Assert.Equal(lookups, Lookups(pve));

        clock.Now += TimeSpan.FromSeconds(2);
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
    }

    static int VmlistReads(FakePve pve)
    {
        lock (pve.Calls) return pve.Reads.Count(r => r == ".vmlist");
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task Concurrent_callers_on_a_stale_handle_share_one_recheck(bool stillInRange)
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        clock.Now += TimeSpan.FromSeconds(6);
        var hold = new Hold("read", ".vmlist");
        pve.Gate = hold.Gate;
        var first = Task.Run(() => px.Get("ludius-win10-01"));
        await hold.Reached;
        var others = Enumerable.Range(0, 8).Select(_ => Task.Run(() => px.Get("ludius-win10-01"))).ToArray();
        await Task.Delay(300);
        Assert.All(others, t => Assert.False(t.IsCompleted));
        if (!stillInRange) lock (pve.Calls) Entry(pve, 101).Remove("pool");
        hold.Release();

        if (stillInRange)
            foreach (var t in others.Prepend(first)) Assert.Same(vm, await t);
        else
            foreach (var t in others.Prepend(first)) await Assert.ThrowsAsync<UnauthorizedAccessException>(() => t);
        Assert.Equal(1, VmlistReads(pve));
        Assert.Equal(1, Lookups(pve));
    }

    [Fact]
    public async Task Concurrent_first_lookups_share_one_resolve()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var hold = new Hold("get", "/cluster/resources");
        pve.Gate = hold.Gate;
        var first = Task.Run(() => px.Get("ludius-win10-01"));
        await hold.Reached;
        var others = Enumerable.Range(0, 8).Select(_ => Task.Run(() => px.Get("ludius-win10-01"))).ToArray();
        await Task.Delay(300);
        hold.Release();
        var vm = await first;
        foreach (var t in others) Assert.Same(vm, await t);
        Assert.Equal(1, Lookups(pve));
    }

    [Fact]
    public void A_config_with_pending_changes_is_rechecked_through_pvesh()
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        pve.Pending.Add(101);
        clock.Now += TimeSpan.FromSeconds(6);
        var reads = ConfigReads(pve, 101);
        Assert.Same(vm, px.Get("ludius-win10-01"));
        Assert.Equal(reads + 1, ConfigReads(pve, 101));

        Entry(pve, 101).Remove("pool");
        clock.Now += TimeSpan.FromSeconds(6);
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
    }

    [Theory]
    [InlineData("[pending]")]
    [InlineData("[PENDING] ")]
    [InlineData("[Pending]\t\r")]
    public void Every_pending_header_spelling_goes_to_pvesh(string header)
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        var vm = px.Get("ludius-win10-01");
        pve.ConfTail[101] = $"\n{header}\nargs: -device vhost-vsock-pci,guest-cid=999\n";
        clock.Now += TimeSpan.FromSeconds(6);
        var reads = ConfigReads(pve, 101);
        Assert.Same(vm, px.Get("ludius-win10-01"));
        Assert.Equal(reads + 1, ConfigReads(pve, 101));
    }

    [Theory]
    [InlineData("\n[bad header]\nmemory: 1\n")]
    [InlineData("\n[1snap]\n")]
    [InlineData("\n[special:other]\n")]
    [InlineData("\n[clean\n")]
    [InlineData("name: ludius-win10-01\n")]
    public void A_config_pvesh_would_read_differently_is_refused(string tail)
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        pve.ConfTail[101] = tail;
        clock.Now += TimeSpan.FromSeconds(6);
        Assert.Throws<IOException>(() => px.Get("ludius-win10-01"));
        Assert.Throws<IOException>(() => px.ListVms());
    }

    [Theory]
    [InlineData("pool:other::200,101::\n")]
    [InlineData(" pool:other::101::\n")]
    [InlineData("POOL:other::101::\n")]
    [InlineData("pool:ludius::101::\n")]
    public void A_vmid_with_conflicting_pool_memberships_is_refused(string tail)
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        pve.UserCfgTail = tail;
        clock.Now += TimeSpan.FromSeconds(6);
        Assert.Throws<IOException>(() => px.Get("ludius-win10-01"));
    }

    [Fact]
    public void A_vmid_reused_by_another_vm_is_refused()
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        pve.Ctime[101] = 1800000000;
        clock.Now += TimeSpan.FromSeconds(6);
        var e = Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
        Assert.Contains("VMID reused", e.Message);

        var fresh = px.Get("ludius-win10-01");
        Assert.Equal(Proxmox.IdentityOf(JsonNode.Parse("""{"meta": "creation-qemu=9.0.2,ctime=1800000000", "smbios1": "uuid=00000065-1111-2222-3333-444455556666"}""")), fresh.Identity);
    }

    [Fact]
    public void A_vmid_now_holding_a_non_range_vm_is_refused()
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        var e = Entry(pve, 101);
        e["name"] = "web-prod-2";
        e.Remove("pool");
        pve.Ctime[101] = 1800000000;
        clock.Now += TimeSpan.FromSeconds(6);
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
        Assert.Throws<KeyNotFoundException>(() => px.Get("ludius-win10-01"));
        Assert.Empty(qmp.Commands);
    }

    [Fact]
    public async Task Lifecycle_checks_are_always_fresh()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        Entry(pve, 101).Remove("pool");
        foreach (var (op, cp, target) in new[] { (Op.Start, "", ""), (Op.Stop, "", ""), (Op.Checkpoint, "x", ""),
                     (Op.Restore, "clean", ""), (Op.Clone, "clean", "ludius-new"), (Op.Delete, "", "ludius-win10-01") })
        {
            var lookups = Lookups(pve);
            await Assert.ThrowsAsync<UnauthorizedAccessException>(() => px.Lifecycle("ludius-win10-01", op, cp, target, CancellationToken.None));
            Assert.Equal(lookups + 1, Lookups(pve));
        }
        Assert.DoesNotContain(pve.Calls, c => c.Method != "get");
    }

    sealed class Hold(string method, string path)
    {
        readonly TaskCompletionSource reached = new(TaskCreationOptions.RunContinuationsAsynchronously);
        readonly TaskCompletionSource release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        int used;
        public Task Reached => reached.Task.WaitAsync(TimeSpan.FromSeconds(10));
        public void Release() => release.TrySetResult();

        public async Task Gate(string m, string p)
        {
            if (m != method || p != path || Interlocked.Exchange(ref used, 1) == 1) return;
            reached.TrySetResult();
            await release.Task.WaitAsync(TimeSpan.FromSeconds(10));
        }
    }

    static int ConfigReads(FakePve pve, int vmid)
    {
        lock (pve.Calls) return pve.Calls.Count(c => c.Method == "get" && c.Path == $"/nodes/pve1/qemu/{vmid}/config");
    }

    [Fact]
    public async Task A_lookup_that_raced_a_delete_never_caches_its_handle()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var hold = new Hold("get", "/nodes/pve1/qemu/101/config");
        pve.Gate = hold.Gate;
        var lookup = Task.Run(() => px.Get("ludius-win10-01"));
        await hold.Reached;
        await px.Lifecycle("ludius-win10-01", Op.Delete, "", "ludius-win10-01", CancellationToken.None);
        hold.Release();
        var e = await Assert.ThrowsAsync<IOException>(() => lookup);
        Assert.Contains("during the lookup", e.Message);

        var reads = ConfigReads(pve, 101);
        px.Get("ludius-win10-01");
        Assert.Equal(reads + 1, ConfigReads(pve, 101));
    }

    static JsonNode Clone104() =>
        JsonNode.Parse("""{"vmid": 104, "name": "ludius-win10-02", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}""")!;

    [Fact]
    public async Task No_handle_on_a_clone_before_its_own_vsock_cid_is_set()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var hold = new Hold("set", "/nodes/pve1/qemu/104/config");
        pve.Gate = async (m, p) =>
        {
            if (m == "create" && p == "/nodes/pve1/qemu/9000/clone") lock (pve.Calls) pve.Vms.Add(Clone104());
            await hold.Gate(m, p);
        };
        pve.Args[104] = pve.Args[9000];
        var clone = px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        await hold.Reached;

        var e = Assert.Throws<IOException>(() => px.Get("ludius-win10-02"));
        Assert.Contains("during the lookup", e.Message);
        hold.Release();
        await clone;

        uint? cid = null;
        px.ConnectGuest = (c, ct) => { cid = c; throw new IOException("no guest in this test"); };
        var vm = px.Get("ludius-win10-02");
        await Assert.ThrowsAsync<IOException>(() => vm.Channel.Request(new ObserverRequest { Ping = new Ping() }, TimeSpan.FromSeconds(1), CancellationToken.None));
        Assert.Equal(104u, cid);
    }

    [Fact]
    public async Task A_lookup_that_overlapped_clone_configuration_is_refused_after_it()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var cloneHold = new Hold("set", "/nodes/pve1/qemu/104/config");
        var lookupHold = new Hold("get", "/nodes/pve1/qemu/104/config");
        var clone104 = false;
        pve.Gate = async (m, p) =>
        {
            if (m == "create" && p == "/nodes/pve1/qemu/9000/clone") lock (pve.Calls) pve.Vms.Add(Clone104());
            if (m == "set") await cloneHold.Gate(m, p);
            else if (Volatile.Read(ref clone104)) await lookupHold.Gate(m, p);
        };
        pve.Args[104] = pve.Args[9000];
        var clone = px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        await cloneHold.Reached;
        Volatile.Write(ref clone104, true);
        var lookup = Task.Run(() => px.Get("ludius-win10-02"));
        await lookupHold.Reached;
        cloneHold.Release();
        await clone;
        lookupHold.Release();
        await Assert.ThrowsAsync<IOException>(() => lookup);

        var reads = ConfigReads(pve, 104);
        px.Get("ludius-win10-02");
        Assert.Equal(reads + 1, ConfigReads(pve, 104));
    }

    [Theory]
    [InlineData("fails")]
    [InlineData("cancelled")]
    public async Task A_clone_left_with_its_source_cid_is_refused(string how)
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        using var cts = new CancellationTokenSource();
        pve.Gate = (m, p) =>
        {
            if (m == "create" && p == "/nodes/pve1/qemu/9000/clone") lock (pve.Calls) pve.Vms.Add(Clone104());
            if (m != "set") return Task.CompletedTask;
            if (how == "fails") throw new IOException("pvesh: 500 config locked");
            cts.Cancel();
            throw new OperationCanceledException(cts.Token);
        };
        pve.Args[104] = pve.Args[9000];
        await Assert.ThrowsAnyAsync<Exception>(() => px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", cts.Token));
        pve.Gate = null;

        var connects = 0;
        px.ConnectGuest = (c, ct) => { connects++; throw new IOException("no guest in this test"); };
        foreach (var _ in new[] { 1, 2 })
        {
            var e = Assert.Throws<IOException>(() => px.Get("ludius-win10-02"));
            Assert.Contains("vsock CID 9000, not its VMID", e.Message);
            clock.Now += TimeSpan.FromSeconds(6);
        }
        Assert.Equal(0, connects);

        pve.Args[104] = "-device vhost-vsock-pci,guest-cid=104";
        Assert.Equal(104, px.Get("ludius-win10-02").Id);
    }

    [Fact]
    public async Task A_clone_whose_cid_did_not_take_reports_failure()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        pve.Gate = (m, p) =>
        {
            if (m == "create" && p == "/nodes/pve1/qemu/9000/clone") lock (pve.Calls) pve.Vms.Add(Clone104());
            return Task.CompletedTask;
        };
        pve.Args[104] = pve.Args[9000];
        pve.IgnoreSet = true;
        var e = await Assert.ThrowsAsync<IOException>(() => px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None));
        Assert.Contains("did not take its vsock CID", e.Message);
        Assert.Throws<IOException>(() => px.Get("ludius-win10-02"));
    }

    [Fact]
    public async Task One_clone_does_not_clear_another_clones_mark()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var readBack = new Hold("get", "/nodes/pve1/qemu/104/config");
        var sets = 0;
        pve.Gate = async (m, p) =>
        {
            if (m == "create" && p == "/nodes/pve1/qemu/9000/clone") lock (pve.Calls) pve.Vms.Add(Clone104());
            if (m == "create" && p == "/nodes/pve1/qemu/101/clone") throw new IOException("pvesh: VM 104 already exists");
            if (m == "set") Interlocked.Increment(ref sets);
            if (Volatile.Read(ref sets) == 1) await readBack.Gate(m, p);
        };
        pve.Args[104] = pve.Args[9000];
        var first = px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        await readBack.Reached;
        await Assert.ThrowsAsync<IOException>(() => px.Lifecycle("ludius-win10-01", Op.Clone, "clean", "ludius-win10-03", CancellationToken.None));

        var e = Assert.Throws<IOException>(() => px.Get("ludius-win10-02"));
        Assert.Contains("during the lookup", e.Message);
        readBack.Release();
        await first;
        Assert.Equal(104, px.Get("ludius-win10-02").Id);
    }

    [Fact]
    public async Task Delete_and_clone_drop_the_affected_handles_at_once()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var old = px.Get("ludius-win10-01");
        await px.Lifecycle("ludius-win10-01", Op.Delete, "", "ludius-win10-01", CancellationToken.None);
        Assert.NotSame(old, px.Get("ludius-win10-01"));

        pve.Vms.Add(JsonNode.Parse("""{"vmid": 104, "name": "ludius-win10-02", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}"""));
        var stale = px.Get("ludius-win10-02");
        pve.Vms.Remove(pve.Vms.Single(v => (int)v!["vmid"]! == 104));
        await px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        pve.Vms.Add(JsonNode.Parse("""{"vmid": 104, "name": "ludius-win10-02", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}"""));
        Assert.NotSame(stale, px.Get("ludius-win10-02"));
    }
}
