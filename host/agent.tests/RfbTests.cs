using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using Ludius.HostAgent;
using Xunit;

namespace Ludius.HostAgent.Tests;

// A QEMU VNC server as the monitor sees it: RFB 3.8, security None, raw full-screen updates where pixel (x, y) is
// B = x, G = y, R = Fill. It records every client message type and request; anything but SetPixelFormat (0),
// SetEncodings (2) and FramebufferUpdateRequest (3) after the handshake is a violation and ends the connection.
sealed class FakeRfb : IDisposable
{
    public readonly int W, H;
    public byte Fill = 0x40;
    public volatile bool Changing; // answer incremental requests too (a desktop that keeps changing)
    public volatile bool Mute; // answer no request at all: a QEMU that stops answering without closing
    public volatile bool SilentHandshake; // accept, then never greet
    public int Closed; // sessions the client ended
    public readonly List<byte> ClientTypes = [];
    public readonly List<bool> Incremental = [];
    public string? Violation;
    public int Connections;
    readonly TcpListener listener = new(IPAddress.Loopback, 0);

    public FakeRfb(int w, int h)
    {
        (W, H) = (w, h);
        listener.Start();
        _ = Task.Run(Serve);
    }

    public async Task<Stream> Connect(CancellationToken ct)
    {
        var c = new TcpClient();
        await c.ConnectAsync((IPEndPoint)listener.LocalEndpoint, ct);
        return c.GetStream();
    }

    async Task Serve()
    {
        while (true)
        {
            TcpClient c;
            try { c = await listener.AcceptTcpClientAsync(); } catch { return; }
            Interlocked.Increment(ref Connections);
            _ = Task.Run(async () =>
            {
                try { await Session(c.GetStream()); }
                catch (Exception e) when (e is IOException or ObjectDisposedException or EndOfStreamException) { }
                finally { c.Dispose(); Interlocked.Increment(ref Closed); }
            });
        }
    }

    async Task Session(NetworkStream s)
    {
        async Task<byte[]> Read(int n) { var b = new byte[n]; await s.ReadExactlyAsync(b); return b; }
        if (SilentHandshake) { await Read(1); return; }
        await s.WriteAsync("RFB 003.008\n"u8.ToArray());
        if (System.Text.Encoding.ASCII.GetString(await Read(12)) != "RFB 003.008\n") { Violation = "version"; return; }
        await s.WriteAsync(new byte[] { 2, 2, 1 }); // VNC auth and None offered: the client must pick None
        if ((await Read(1))[0] != 1) { Violation = "security"; return; }
        await s.WriteAsync(new byte[4]);
        if ((await Read(1))[0] != 1) { Violation = "not shared"; return; }
        var init = new byte[24 + 4];
        BinaryPrimitives.WriteUInt16BigEndian(init, (ushort)W);
        BinaryPrimitives.WriteUInt16BigEndian(init.AsSpan(2), (ushort)H);
        BinaryPrimitives.WriteUInt32BigEndian(init.AsSpan(20), 4);
        "fake"u8.CopyTo(init.AsSpan(24));
        await s.WriteAsync(init);
        while (true)
        {
            var t = (await Read(1))[0];
            lock (ClientTypes) ClientTypes.Add(t);
            switch (t)
            {
                case 0: await Read(19); break;
                case 2: await Read(BinaryPrimitives.ReadUInt16BigEndian((await Read(3)).AsSpan(1)) * 4); break;
                case 3:
                    var incremental = (await Read(9))[0] == 1;
                    lock (ClientTypes) Incremental.Add(incremental);
                    if (!Mute && (!incremental || Changing)) await s.WriteAsync(Update());
                    break;
                default: Violation = $"client sent message type {t}"; return;
            }
        }
    }

    byte[] Update()
    {
        var b = new byte[4 + 12 + W * H * 4];
        BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(2), 1);
        BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(8), (ushort)W);
        BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(10), (ushort)H);
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++)
            {
                int i = 16 + (y * W + x) * 4;
                (b[i], b[i + 1], b[i + 2]) = ((byte)x, (byte)y, Fill);
            }
        return b;
    }

    public void Dispose() => listener.Stop();
}

// Only Frame matters to the hub; every frame is a new byte[] like Hyper-V's thumbnails.
sealed class FrameOnlyHv : IHypervisor
{
    int n;
    public string Backend => "fake";
    public byte[] Frame(string name, int maxWidth) => [(byte)Interlocked.Increment(ref n)];
    public VmHandle Get(string name) => throw new NotSupportedException();
    public Task Move(VmHandle vm, int x, int y, uint durationMs, CancellationToken ct) => throw new NotSupportedException();
    public Task Click(VmHandle vm, int button, uint count, CancellationToken ct) => throw new NotSupportedException();
    public void Key(VmHandle vm, uint vk, bool down) => throw new NotSupportedException();
    public Task Chord(VmHandle vm, IReadOnlyList<uint> keys, CancellationToken ct) => throw new NotSupportedException();
    public Task Type(VmHandle vm, string text, uint meanMs, uint jitterMs, CancellationToken ct) => throw new NotSupportedException();
    public void CtrlAltDel(VmHandle vm) => throw new NotSupportedException();
    public Task Scroll(VmHandle vm, int notches, CancellationToken ct) => throw new NotSupportedException();
    public Task<string> Lifecycle(string vm, Ludius.V1.LifecycleRequest.Types.Op op, string checkpoint, string target, CancellationToken ct) => throw new NotSupportedException();
    public (byte[] Png, int Width, int Height) Screenshot(VmHandle vm) => throw new NotSupportedException();
    public List<VmInfo> ListVms() => throw new NotSupportedException();
}

public class RfbTests
{
    static readonly CancellationToken Ct = CancellationToken.None;

    [Fact]
    public async Task Handshake_then_raw_updates_land_in_the_framebuffer()
    {
        using var server = new FakeRfb(64, 32) { Changing = true };
        await using var s = await server.Connect(Ct);
        var client = await RfbClient.Connect(s, Ct);
        Assert.Equal((64, 32), (client.Width, client.Height));
        using var stop = CancellationTokenSource.CreateLinkedTokenSource(Ct);
        var updates = 0;
        var three = new TaskCompletionSource();
        var run = client.Run(TimeSpan.Zero, TimeSpan.FromSeconds(10), () => { if (++updates == 3) three.TrySetResult(); }, stop.Token);
        await three.Task.WaitAsync(TimeSpan.FromSeconds(10), Ct);
        lock (client.Lock)
        {
            int i = (7 * 64 + 5) * 4;
            Assert.Equal(((byte)5, (byte)7, (byte)0x40), (client.Pixels[i], client.Pixels[i + 1], client.Pixels[i + 2]));
            Assert.True(client.Version >= 3);
        }
        lock (server.ClientTypes) Assert.Equal(new[] { false, true, true }, server.Incremental.Take(3));
        stop.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => run);
    }

    [Fact]
    public async Task Client_never_sends_anything_but_format_encodings_and_update_requests()
    {
        // Structural: the writer has no method that could emit KeyEvent, PointerEvent or ClientCutText.
        var methods = typeof(RfbWriter).GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
            .Select(m => m.Name).Where(n => !n.StartsWith('<')).Order().ToArray();
        Assert.Equal(new[] { "ClientInit", "SecurityNone", "SetEncodings", "SetPixelFormat", "UpdateRequest", "Version", "Write" }, methods);
        Assert.Single(typeof(RfbClient).GetFields(BindingFlags.Instance | BindingFlags.NonPublic), f => f.FieldType == typeof(RfbWriter));

        // On the wire, through the shared capture.
        using var server = new FakeRfb(64, 32) { Changing = true };
        var captures = new Captures { Interval = TimeSpan.FromMilliseconds(5) };
        for (int i = 0; i < 20; i++) captures.Frame("ludius-a", "1", server.Connect, 64);
        await Task.Delay(200, Ct);
        Assert.Null(server.Violation);
        lock (server.ClientTypes)
        {
            Assert.True(server.ClientTypes.Count(t => t == 3) > 2);
            Assert.All(server.ClientTypes, t => Assert.Contains(t, new byte[] { 0, 2, 3 }));
        }
    }

    [Fact]
    public void One_capture_serves_every_width_and_encodes_each_once_per_update()
    {
        using var server = new FakeRfb(1280, 720);
        var captures = new Captures();
        var small = captures.Frame("ludius-a", "100|x", server.Connect, 320);
        var large = captures.Frame("ludius-a", "100|x", server.Connect, 640);
        Assert.Equal(1, server.Connections);
        Assert.Equal((320, 180), JpegSize(small));
        Assert.Equal((640, 360), JpegSize(large));
        Assert.Same(small, captures.Frame("ludius-a", "100|x", server.Connect, 320)); // nothing changed: no new encode
        Assert.Equal((1280, 720), JpegSize(captures.Frame("ludius-a", "100|x", server.Connect, 1920))); // never upscaled

        captures.Frame("ludius-a", "101|y", server.Connect, 320); // VMID reused by another VM: a new session
        Assert.Equal(2, server.Connections);
    }

    [Fact]
    public async Task A_viewer_that_stops_reading_does_not_stall_the_others()
    {
        var hub = new FeedHub(new FrameOnlyHv());
        using var stuck = hub.Subscribe("ludius-a", 640); // never read
        using var live = hub.Subscribe("ludius-a", 640);
        var seen = new HashSet<byte>();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(Ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(10));
        while (seen.Count < 10) seen.Add((await live.Reader.ReadAsync(timeout.Token)).Jpeg![0]);
        Assert.True(stuck.Reader.Count <= 1); // its slot holds the latest frame only
    }

    [Fact]
    public async Task A_handshake_that_never_finishes_is_closed_without_any_viewer()
    {
        using var server = new FakeRfb(64, 32) { SilentHandshake = true };
        var captures = new Captures { Handshake = TimeSpan.FromMilliseconds(300), FirstFrame = TimeSpan.FromMilliseconds(100), IdleAfter = TimeSpan.FromMinutes(5) };
        Assert.Equal("no console frame yet", Assert.Throws<InvalidOperationException>(() => captures.Frame("ludius-a", "1", server.Connect, 64)).Message);
        await Until(() => Volatile.Read(ref server.Closed) == 1); // no further request: the deadline alone ends it
        Assert.Equal("console capture did not start", Assert.Throws<InvalidOperationException>(() => captures.Frame("ludius-a", "1", server.Connect, 64)).Message);
    }

    [Fact]
    public async Task A_session_that_stops_answering_reports_stalled_instead_of_its_last_frame()
    {
        using var server = new FakeRfb(64, 32); // an unchanged desktop: only full requests get answers
        var captures = new Captures { Probe = TimeSpan.FromMilliseconds(100), Stall = TimeSpan.FromMilliseconds(400) };
        captures.Frame("ludius-a", "1", server.Connect, 64);
        for (var end = DateTime.UtcNow.AddSeconds(1); DateTime.UtcNow < end; await Task.Delay(50, Ct))
            captures.Frame("ludius-a", "1", server.Connect, 64); // healthy and still: the probes keep it alive
        lock (server.ClientTypes) Assert.True(server.Incremental.Count(i => !i) > 3);
        Assert.Equal(1, server.Connections);

        server.Mute = true;
        await Until(() =>
        {
            try { captures.Frame("ludius-a", "1", server.Connect, 64); return false; } // the last frame, until the deadline
            catch (InvalidOperationException e) { Assert.Equal("capture stalled", e.Message); return true; }
        });
        await Until(() => Volatile.Read(ref server.Closed) == 1);
        Assert.Throws<InvalidOperationException>(() => captures.Frame("ludius-a", "1", server.Connect, 64)); // a new session, still unanswered: no old frame
    }

    [Fact]
    public async Task Close_ends_a_session_at_once()
    {
        using var server = new FakeRfb(64, 32);
        var captures = new Captures();
        captures.Frame("ludius-a", "1", server.Connect, 64);
        captures.Close("ludius-a");
        await Until(() => Volatile.Read(ref server.Closed) == 1);
        captures.Frame("ludius-a", "1", server.Connect, 64);
        Assert.Equal(2, server.Connections);
    }

    static async Task<bool> Until(Func<bool> done)
    {
        for (var end = DateTime.UtcNow.AddSeconds(5); DateTime.UtcNow < end; await Task.Delay(20, Ct))
            if (done()) return true;
        throw new Xunit.Sdk.XunitException("condition not met within 5 s");
    }

    static (int W, int H) JpegSize(byte[] jpeg)
    {
        Assert.Equal(new byte[] { 0xFF, 0xD8 }, jpeg[..2]);
        Assert.Equal(new byte[] { 0xFF, 0xD9 }, jpeg[^2..]);
        for (int i = 2; i + 9 < jpeg.Length; i++)
            if (jpeg[i] == 0xFF && jpeg[i + 1] == 0xC0)
                return (BinaryPrimitives.ReadUInt16BigEndian(jpeg.AsSpan(i + 7)), BinaryPrimitives.ReadUInt16BigEndian(jpeg.AsSpan(i + 5)));
        throw new Xunit.Sdk.XunitException("no SOF0");
    }
}
