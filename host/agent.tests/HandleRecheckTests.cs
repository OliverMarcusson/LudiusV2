using System.Text.Json.Nodes;
using Ludius.HostAgent;
using Xunit;
using Op = Ludius.V1.LifecycleRequest.Types.Op;

namespace Ludius.HostAgent.Tests;

// Cached VM handles (input, observation, screenshots, file reads) are rechecked once older than Recheck; lifecycle is
// resolved fresh on every call. A VM that left the range pool, or a VMID reused by another VM, is refused.
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

        clock.Now += TimeSpan.FromSeconds(4); // within the recheck interval: the cached handle, no API round trip
        var lookups = Lookups(pve);
        Assert.Same(vm, px.Get("ludius-win10-01"));
        Assert.Equal(lookups, Lookups(pve));

        clock.Now += TimeSpan.FromSeconds(2);
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
        Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01")); // dropped: resolved afresh, still refused
    }

    [Fact]
    public void A_vmid_reused_by_another_vm_is_refused()
    {
        var (px, pve, qmp, clock) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01");
        // VM 101 deleted outside the agent and a new VM created with the same VMID, name and even pool.
        pve.Ctime[101] = 1800000000;
        clock.Now += TimeSpan.FromSeconds(6);
        var e = Assert.Throws<UnauthorizedAccessException>(() => px.Get("ludius-win10-01"));
        Assert.Contains("VMID reused", e.Message);

        // A fresh resolve finds the new VM by name in the pool: that one is a range VM in its own right.
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
        Assert.Throws<KeyNotFoundException>(() => px.Get("ludius-win10-01")); // that name no longer exists anywhere
        Assert.Empty(qmp.Commands);
    }

    [Fact]
    public async Task Lifecycle_checks_are_always_fresh()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        px.Get("ludius-win10-01"); // a freshly checked handle in the cache
        Entry(pve, 101).Remove("pool");
        foreach (var (op, cp, target) in new[] { (Op.Start, "", ""), (Op.Stop, "", ""), (Op.Checkpoint, "x", ""),
                     (Op.Restore, "clean", ""), (Op.Clone, "clean", "ludius-new"), (Op.Delete, "", "ludius-win10-01") })
        {
            var lookups = Lookups(pve);
            await Assert.ThrowsAsync<UnauthorizedAccessException>(() => px.Lifecycle("ludius-win10-01", op, cp, target, CancellationToken.None));
            Assert.Equal(lookups + 1, Lookups(pve)); // looked up now, not taken from the handle
        }
        Assert.DoesNotContain(pve.Calls, c => c.Method != "get");
    }

    [Fact]
    public async Task Delete_and_clone_drop_the_affected_handles_at_once()
    {
        var (px, pve, qmp, _) = Make();
        using var _q = qmp;
        var old = px.Get("ludius-win10-01");
        await px.Lifecycle("ludius-win10-01", Op.Delete, "", "ludius-win10-01", CancellationToken.None);
        Assert.NotSame(old, px.Get("ludius-win10-01")); // resolved afresh, not the deleted VM's handle

        // A handle cached on VMID 104, then that VM deleted outside the agent: the clone that reuses 104 drops it.
        pve.Vms.Add(JsonNode.Parse("""{"vmid": 104, "name": "ludius-win10-02", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}"""));
        var stale = px.Get("ludius-win10-02");
        pve.Vms.Remove(pve.Vms.Single(v => (int)v!["vmid"]! == 104));
        await px.Lifecycle("ludius-golden", Op.Clone, "", "ludius-win10-02", CancellationToken.None);
        pve.Vms.Add(JsonNode.Parse("""{"vmid": 104, "name": "ludius-win10-02", "status": "stopped", "node": "pve1", "type": "qemu", "pool": "ludius"}"""));
        Assert.NotSame(stale, px.Get("ludius-win10-02"));
    }
}
