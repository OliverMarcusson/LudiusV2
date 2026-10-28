using System.Buffers.Binary;
using System.Runtime.ExceptionServices;
using System.Text;

namespace Ludius.HostAgent;

// The monitor's console capture on Proxmox: one view-only RFB (VNC, RFC 6143) client per watched VM, on a private
// socket QEMU was handed over QMP (Qmp.VncClient). Raw encoding (a local socket: bandwidth is free, decoding is not),
// the framebuffer kept here, scaled and JPEG-encoded per requested width. Persona screenshots don't use this.

// Everything the client ever sends. There is deliberately no way to write KeyEvent (4), PointerEvent (5),
// ClientCutText (6) or any other message: the capture is view-only by construction (RfbTests checks this surface).
sealed class RfbWriter(Stream s)
{
    public Task Version(CancellationToken ct) => Write("RFB 003.008\n"u8.ToArray(), ct);
    public Task SecurityNone(CancellationToken ct) => Write([1], ct);
    public Task ClientInit(CancellationToken ct) => Write([1], ct); // shared: Proxmox's own console stays connected

    // 32 bpp, depth 24, little-endian true colour, red 16 green 8 blue 0: pixels arrive as B, G, R, X.
    public Task SetPixelFormat(CancellationToken ct) =>
        Write([0, 0, 0, 0, 32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0], ct);

    public Task SetEncodings(CancellationToken ct) => Write([2, 0, 0, 2, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0x21], ct); // Raw, DesktopSize (-223)

    public Task UpdateRequest(bool incremental, int width, int height, CancellationToken ct)
    {
        var b = new byte[10];
        b[0] = 3;
        b[1] = (byte)(incremental ? 1 : 0);
        BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(6), (ushort)width);
        BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(8), (ushort)height);
        return Write(b, ct);
    }

    async Task Write(byte[] b, CancellationToken ct)
    {
        await s.WriteAsync(b, ct);
        await s.FlushAsync(ct);
    }
}

sealed class RfbClient
{
    const int MaxSide = 8192;

    public readonly object Lock = new(); // Pixels, Width, Height and Version change together under it
    public int Width { get; private set; }
    public int Height { get; private set; }
    public byte[] Pixels { get; private set; } = []; // B, G, R, X rows of Width pixels
    public long Version { get; private set; } // complete framebuffer updates applied

    readonly Stream s;
    readonly RfbWriter writer;
    byte[] rect = [];

    RfbClient(Stream s) => (this.s, writer) = (s, new RfbWriter(s));

    public static async Task<RfbClient> Connect(Stream s, CancellationToken ct)
    {
        var c = new RfbClient(s);
        var version = Encoding.ASCII.GetString(await c.Bytes(12, ct));
        if (!version.StartsWith("RFB 003.") || !int.TryParse(version.AsSpan(8, 3), out var minor) || minor < 8)
            throw new IOException($"RFB: unsupported server version {version.Trim()}");
        await c.writer.Version(ct);
        var n = (await c.Bytes(1, ct))[0];
        if (n == 0) throw new IOException($"RFB: refused: {await c.Reason(ct)}");
        if (!(await c.Bytes(n, ct)).Contains((byte)1)) throw new IOException("RFB: server offers no security type None");
        await c.writer.SecurityNone(ct);
        if (BinaryPrimitives.ReadUInt32BigEndian(await c.Bytes(4, ct)) != 0) throw new IOException($"RFB: security failed: {await c.Reason(ct)}");
        await c.writer.ClientInit(ct);
        var init = await c.Bytes(24, ct);
        var nameLength = BinaryPrimitives.ReadUInt32BigEndian(init.AsSpan(20));
        if (nameLength > 4096) throw new IOException("RFB: desktop name too long");
        await c.Bytes((int)nameLength, ct);
        c.Resize(BinaryPrimitives.ReadUInt16BigEndian(init), BinaryPrimitives.ReadUInt16BigEndian(init.AsSpan(2)));
        await c.writer.SetPixelFormat(ct);
        await c.writer.SetEncodings(ct);
        return c;
    }

    // Requests an update, applies it, waits out the rest of interval, requests the next: at most one update per
    // interval. QEMU answers an incremental request only once something changed, so a still desktop costs nothing;
    // after probe without a message a full (non-incremental) request goes out, which QEMU always answers. HeardAt is
    // when the last update arrived: Captures ends a session that stays silent despite the probes.
    public long HeardAt { get; private set; } = Environment.TickCount64;

    public async Task Run(TimeSpan interval, TimeSpan probe, Action updated, CancellationToken ct)
    {
        var asked = System.Diagnostics.Stopwatch.StartNew();
        await writer.UpdateRequest(false, Width, Height, ct);
        var next = Bytes(1, ct);
        while (true)
        {
            using (var quiet = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                var first = await Task.WhenAny(next, Task.Delay(probe, quiet.Token));
                quiet.Cancel();
                if (first != next)
                {
                    ct.ThrowIfCancellationRequested();
                    await writer.UpdateRequest(false, Width, Height, ct);
                    continue;
                }
            }
            switch ((await next)[0])
            {
                case 0:
                    var resized = await Update(ct);
                    HeardAt = Environment.TickCount64;
                    updated();
                    if (interval - asked.Elapsed is { Ticks: > 0 } wait) await Task.Delay(wait, ct);
                    asked.Restart();
                    await writer.UpdateRequest(!resized, Width, Height, ct); // after a resize: the whole new screen
                    break;
                case 1: // SetColourMapEntries: not sent for true colour, skipped if it is
                    var map = await Bytes(5, ct);
                    await Bytes(BinaryPrimitives.ReadUInt16BigEndian(map.AsSpan(3)) * 6, ct);
                    break;
                case 2: break; // Bell
                case 3: // ServerCutText: read past, never used
                    var cut = await Bytes(7, ct);
                    var length = BinaryPrimitives.ReadUInt32BigEndian(cut.AsSpan(3));
                    if (length > 1 << 20) throw new IOException("RFB: cut text too long");
                    await Bytes((int)length, ct);
                    break;
                case var t: throw new IOException($"RFB: unexpected server message {t}");
            }
            next = Bytes(1, ct);
        }
    }

    // One FramebufferUpdate; true if the desktop changed size.
    async Task<bool> Update(CancellationToken ct)
    {
        var n = BinaryPrimitives.ReadUInt16BigEndian((await Bytes(3, ct)).AsSpan(1));
        var resized = false;
        for (int i = 0; i < n; i++)
        {
            var h = await Bytes(12, ct);
            int x = BinaryPrimitives.ReadUInt16BigEndian(h), y = BinaryPrimitives.ReadUInt16BigEndian(h.AsSpan(2));
            int w = BinaryPrimitives.ReadUInt16BigEndian(h.AsSpan(4)), hh = BinaryPrimitives.ReadUInt16BigEndian(h.AsSpan(6));
            switch (BinaryPrimitives.ReadInt32BigEndian(h.AsSpan(8)))
            {
                case -223: Resize(w, hh); resized = true; break;
                case 0:
                    if (x + w > Width || y + hh > Height) throw new IOException("RFB: rectangle outside the framebuffer");
                    var len = w * hh * 4;
                    if (rect.Length < len) rect = new byte[len];
                    await s.ReadExactlyAsync(rect.AsMemory(0, len), ct);
                    lock (Lock)
                        for (int row = 0; row < hh; row++)
                            Buffer.BlockCopy(rect, row * w * 4, Pixels, ((y + row) * Width + x) * 4, w * 4);
                    break;
                case var e: throw new IOException($"RFB: unrequested encoding {e}");
            }
        }
        lock (Lock) Version++;
        return resized;
    }

    void Resize(int w, int h)
    {
        if (w is < 1 or > MaxSide || h is < 1 or > MaxSide) throw new IOException($"RFB: framebuffer {w}x{h}");
        lock (Lock) (Width, Height, Pixels) = (w, h, new byte[w * h * 4]);
    }

    async Task<string> Reason(CancellationToken ct)
    {
        var length = BinaryPrimitives.ReadUInt32BigEndian(await Bytes(4, ct));
        return Encoding.UTF8.GetString(await Bytes((int)Math.Min(length, 1024), ct));
    }

    async Task<byte[]> Bytes(int n, CancellationToken ct)
    {
        var b = new byte[n];
        await s.ReadExactlyAsync(b, ct);
        return b;
    }
}

// One RFB session per VM, shared by every viewer and width, ended IdleAfter after the last frame request. Each width is
// scaled and encoded once per framebuffer update; until the next update the same byte[] comes back (FeedHub relies on it).
// A watchdog bounds every session from before it connects: no first frame within Handshake, or no update within Stall
// once running (probes included), ends it with an error the next frame request gets, never the last frame as if live.
// Ending a session disposes its socket.
sealed class Captures
{
    public TimeSpan Interval { get; init; } = TimeSpan.FromSeconds(1.0 / 15);
    public TimeSpan IdleAfter { get; init; } = TimeSpan.FromSeconds(10);
    public TimeSpan FirstFrame { get; init; } = TimeSpan.FromSeconds(3); // how long one frame request waits for a new session
    public TimeSpan Handshake { get; init; } = TimeSpan.FromSeconds(10); // connect, handshake and first update
    public TimeSpan Probe { get; init; } = TimeSpan.FromSeconds(2); // silence before a full update request
    public TimeSpan Stall { get; init; } = TimeSpan.FromSeconds(5); // no update for this long, probes included: ended
    const int Quality = 80;

    sealed class Capture(string tag)
    {
        public readonly string Tag = tag;
        public readonly CancellationTokenSource Stop = new();
        public readonly TaskCompletionSource<RfbClient> Ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public long AskedAt = Environment.TickCount64;
        public volatile Exception? Error; // why the session ended, unless idle, replaced or closed
        public readonly Dictionary<int, (long Version, byte[] Jpeg)> Encoded = [];
    }

    readonly Dictionary<string, Capture> live = [];

    // tag: the VM identity the caller just validated. A session only serves the tag it was opened for; another tag for
    // the name ends the old session and opens a new one with the caller's connect.
    public byte[] Frame(string vm, string tag, Func<CancellationToken, Task<Stream>> connect, int maxWidth)
    {
        Capture? c;
        lock (live)
        {
            if (live.TryGetValue(vm, out c) && c.Tag == tag && c.Error is { } failed)
            {
                live.Remove(vm); // reported once; the next request opens a new session
                ExceptionDispatchInfo.Throw(failed);
            }
            if (c is null || c.Tag != tag || c.Stop.IsCancellationRequested)
            {
                c?.Stop.Cancel();
                live[vm] = c = new Capture(tag);
                _ = Run(vm, c, connect);
            }
            Volatile.Write(ref c.AskedAt, Environment.TickCount64);
        }
        try { if (!c.Ready.Task.Wait(FirstFrame)) throw new InvalidOperationException("no console frame yet"); }
        catch (AggregateException e) { ExceptionDispatchInfo.Throw(e.InnerException!); }
        Ended(c);
        var jpeg = Encode(c, c.Ready.Task.Result, maxWidth);
        Ended(c); // it may have ended while this request waited on another width's encode, or during its own
        return jpeg;
    }

    // Ended: its last frame is no longer known to be current.
    static void Ended(Capture c)
    {
        if (c.Stop.IsCancellationRequested)
            throw c.Error as InvalidOperationException ?? new InvalidOperationException("console capture ended");
    }

    // Ends the name's session now (its VM failed validation).
    public void Close(string vm)
    {
        lock (live) if (live.Remove(vm, out var c)) c.Stop.Cancel();
    }

    async Task Run(string vm, Capture c, Func<CancellationToken, Task<Stream>> connect)
    {
        var ct = c.Stop.Token;
        var watchdog = Watch(c);
        try
        {
            await using var s = await connect(ct);
            await using var close = ct.Register(s.Dispose); // also unblocks a read that would never return
            var client = await RfbClient.Connect(s, ct);
            await client.Run(Interval, Probe, () => c.Ready.TrySetResult(client), ct);
        }
        catch (Exception e)
        {
            if (!ct.IsCancellationRequested) c.Error ??= e;
            c.Ready.TrySetException(c.Error ?? e);
        }
        finally
        {
            c.Stop.Cancel();
            c.Ready.TrySetException(new InvalidOperationException("console capture ended"));
            lock (live) if (c.Error is null && live.TryGetValue(vm, out var now) && now == c) live.Remove(vm);
            await watchdog;
        }
    }

    async Task Watch(Capture c)
    {
        var started = Environment.TickCount64;
        while (!c.Stop.IsCancellationRequested)
        {
            await Task.Delay(100);
            var now = Environment.TickCount64;
            var running = c.Ready.Task.IsCompletedSuccessfully;
            if (now - Volatile.Read(ref c.AskedAt) >= IdleAfter.TotalMilliseconds) c.Stop.Cancel();
            else if (running ? now - c.Ready.Task.Result.HeardAt > Stall.TotalMilliseconds : now - started > Handshake.TotalMilliseconds)
            {
                c.Error = new InvalidOperationException(running ? "capture stalled" : "console capture did not start");
                c.Stop.Cancel();
            }
        }
    }

    // ponytail: one encode at a time per VM (widths of one VM wait on each other); per-width locks if a mixed grid lags.
    static byte[] Encode(Capture c, RfbClient client, int maxWidth)
    {
        lock (c.Encoded)
        {
            byte[] rgb;
            int w, h;
            long version;
            lock (client.Lock)
            {
                version = client.Version;
                if (c.Encoded.TryGetValue(maxWidth, out var hit) && hit.Version == version) return hit.Jpeg;
                w = Math.Min(maxWidth, client.Width);
                h = Math.Max(1, (int)Math.Round((double)client.Height * w / client.Width));
                rgb = Jpeg.Scale(client.Pixels, client.Width, client.Height, w, h);
            }
            var jpeg = Jpeg.Encode(rgb, w, h, Quality);
            c.Encoded[maxWidth] = (version, jpeg);
            return jpeg;
        }
    }
}

// Baseline JPEG (4:2:0, standard Annex K tables) and an area-average downscale: enough for a view-only feed, and no
// image library on linux-x64 (System.Drawing is Windows-only).
static class Jpeg
{
    // B, G, R, X at sw x sh to R, G, B at w x h (w <= sw): each output pixel averages the source pixels it covers.
    public static byte[] Scale(byte[] bgrx, int sw, int sh, int w, int h)
    {
        var rgb = new byte[w * h * 3];
        for (int oy = 0; oy < h; oy++)
        {
            int y0 = (int)((long)oy * sh / h), y1 = Math.Max(y0 + 1, (int)((long)(oy + 1) * sh / h));
            for (int ox = 0; ox < w; ox++)
            {
                int x0 = (int)((long)ox * sw / w), x1 = Math.Max(x0 + 1, (int)((long)(ox + 1) * sw / w));
                int r = 0, g = 0, b = 0;
                for (int y = y0; y < y1; y++)
                    for (int i = (y * sw + x0) * 4, end = (y * sw + x1) * 4; i < end; i += 4)
                    {
                        b += bgrx[i];
                        g += bgrx[i + 1];
                        r += bgrx[i + 2];
                    }
                int n = (y1 - y0) * (x1 - x0), o = (oy * w + ox) * 3;
                rgb[o] = (byte)(r / n);
                rgb[o + 1] = (byte)(g / n);
                rgb[o + 2] = (byte)(b / n);
            }
        }
        return rgb;
    }

    static readonly byte[] Zigzag =
    [
        0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
        35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
    ];

    static readonly byte[] LumaQ =
    [
        16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
        18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
    ];

    static readonly byte[] ChromaQ =
    [
        17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
        99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    ];

    static readonly byte[] DcLumaBits = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0];
    static readonly byte[] DcChromaBits = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0];
    static readonly byte[] DcValues = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
    static readonly byte[] AcLumaBits = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d];
    static readonly byte[] AcLumaValues =
    [
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
        0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
        0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
        0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
    ];
    static readonly byte[] AcChromaBits = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77];
    static readonly byte[] AcChromaValues =
    [
        0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
        0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0, 0x15, 0x62, 0x72, 0xd1, 0x0a, 0x16, 0x24, 0x34, 0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
        0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
        0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
        0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
        0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
    ];

    static readonly (ushort Code, byte Length)[] DcLuma = Codes(DcLumaBits, DcValues), DcChroma = Codes(DcChromaBits, DcValues),
        AcLuma = Codes(AcLumaBits, AcLumaValues), AcChroma = Codes(AcChromaBits, AcChromaValues);

    static readonly float[] Aan = [1f, 1.387039845f, 1.306562965f, 1.175875602f, 1f, 0.785694958f, 0.541196100f, 0.275899379f];

    // Canonical Huffman codes by symbol, from the per-length counts.
    static (ushort, byte)[] Codes(byte[] bits, byte[] values)
    {
        var codes = new (ushort, byte)[256];
        int code = 0, k = 0;
        for (int len = 1; len <= 16; len++, code <<= 1)
            for (int i = 0; i < bits[len - 1]; i++) codes[values[k++]] = ((ushort)code++, (byte)len);
        return codes;
    }

    // IJG quality scaling; the table stays in natural order, written out in zigzag.
    static byte[] Scaled(byte[] table, int quality)
    {
        int s = quality < 50 ? 5000 / quality : 200 - quality * 2;
        return table.Select(q => (byte)Math.Clamp((q * s + 50) / 100, 1, 255)).ToArray();
    }

    // Quantisation divisors folded with the AAN DCT's output scale.
    static float[] Divisors(byte[] q)
    {
        var d = new float[64];
        for (int i = 0; i < 64; i++) d[i] = 1f / (q[i] * Aan[i / 8] * Aan[i % 8] * 8f);
        return d;
    }

    public static byte[] Encode(byte[] rgb, int w, int h, int quality)
    {
        var lq = Scaled(LumaQ, quality);
        var cq = Scaled(ChromaQ, quality);
        var o = new BitWriter(w * h / 4 + 1024);
        o.Bytes(0xFF, 0xD8, 0xFF, 0xE0, 0, 16, (byte)'J', (byte)'F', (byte)'I', (byte)'F', 0, 1, 1, 0, 0, 1, 0, 1, 0, 0);
        o.Bytes(0xFF, 0xDB, 0, 132, 0);
        foreach (var z in Zigzag) o.Bytes(lq[z]);
        o.Bytes(1);
        foreach (var z in Zigzag) o.Bytes(cq[z]);
        o.Bytes(0xFF, 0xC0, 0, 17, 8, (byte)(h >> 8), (byte)h, (byte)(w >> 8), (byte)w, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1);
        o.Bytes(0xFF, 0xC4, 418 >> 8, 418 & 0xFF); // 2 + 4 x (class + 16 counts) + 12 + 162 + 12 + 162 values
        foreach (var (cls, bits, values) in new[] { (0x00, DcLumaBits, DcValues), (0x10, AcLumaBits, AcLumaValues), (0x01, DcChromaBits, DcValues), (0x11, AcChromaBits, AcChromaValues) })
        {
            o.Bytes((byte)cls);
            o.Bytes(bits);
            o.Bytes(values);
        }
        o.Bytes(0xFF, 0xDA, 0, 12, 3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0);

        var ld = Divisors(lq);
        var cd = Divisors(cq);
        var Y = new float[256];
        var Cb = new float[256];
        var Cr = new float[256];
        var block = new float[64];
        int dcY = 0, dcCb = 0, dcCr = 0;
        for (int my = 0; my < h; my += 16)
            for (int mx = 0; mx < w; mx += 16)
            {
                for (int y = 0; y < 16; y++)
                    for (int x = 0; x < 16; x++)
                    {
                        int i = (Math.Min(my + y, h - 1) * w + Math.Min(mx + x, w - 1)) * 3; // edges repeat
                        float r = rgb[i], g = rgb[i + 1], b = rgb[i + 2];
                        Y[y * 16 + x] = 0.299f * r + 0.587f * g + 0.114f * b - 128f;
                        Cb[y * 16 + x] = -0.168736f * r - 0.331264f * g + 0.5f * b;
                        Cr[y * 16 + x] = 0.5f * r - 0.418688f * g - 0.081312f * b;
                    }
                for (int by = 0; by < 16; by += 8)
                    for (int bx = 0; bx < 16; bx += 8)
                    {
                        for (int i = 0; i < 64; i++) block[i] = Y[(by + i / 8) * 16 + bx + i % 8];
                        dcY = Block(o, block, ld, dcY, DcLuma, AcLuma);
                    }
                foreach (var (plane, isCb) in new[] { (Cb, true), (Cr, false) })
                {
                    for (int i = 0; i < 64; i++)
                    {
                        int p = (i / 8) * 32 + (i % 8) * 2;
                        block[i] = (plane[p] + plane[p + 1] + plane[p + 16] + plane[p + 17]) / 4f;
                    }
                    if (isCb) dcCb = Block(o, block, cd, dcCb, DcChroma, AcChroma);
                    else dcCr = Block(o, block, cd, dcCr, DcChroma, AcChroma);
                }
            }
        o.Flush();
        o.Bytes(0xFF, 0xD9);
        return o.ToArray();
    }

    // Forward DCT (AAN, float), quantise, Huffman-code one 8x8 block; returns its DC for the next block's difference.
    static int Block(BitWriter o, float[] d, float[] divisors, int lastDc, (ushort, byte)[] dc, (ushort, byte)[] ac)
    {
        for (int pass = 0; pass < 2; pass++)
            for (int k = 0; k < 8; k++)
            {
                int b = pass == 0 ? k * 8 : k, s = pass == 0 ? 1 : 8; // rows, then columns
                float t0 = d[b] + d[b + 7 * s], t7 = d[b] - d[b + 7 * s], t1 = d[b + s] + d[b + 6 * s], t6 = d[b + s] - d[b + 6 * s];
                float t2 = d[b + 2 * s] + d[b + 5 * s], t5 = d[b + 2 * s] - d[b + 5 * s], t3 = d[b + 3 * s] + d[b + 4 * s], t4 = d[b + 3 * s] - d[b + 4 * s];
                float t10 = t0 + t3, t13 = t0 - t3, t11 = t1 + t2, t12 = t1 - t2;
                d[b] = t10 + t11;
                d[b + 4 * s] = t10 - t11;
                float z1 = (t12 + t13) * 0.707106781f;
                d[b + 2 * s] = t13 + z1;
                d[b + 6 * s] = t13 - z1;
                t10 = t4 + t5;
                t11 = t5 + t6;
                t12 = t6 + t7;
                float z5 = (t10 - t12) * 0.382683433f, z2 = 0.541196100f * t10 + z5, z4 = 1.306562965f * t12 + z5, z3 = t11 * 0.707106781f;
                float z11 = t7 + z3, z13 = t7 - z3;
                d[b + 5 * s] = z13 + z2;
                d[b + 3 * s] = z13 - z2;
                d[b + s] = z11 + z4;
                d[b + 7 * s] = z11 - z4;
            }
        Span<int> q = stackalloc int[64];
        for (int i = 0; i < 64; i++) q[i] = (int)MathF.Round(d[Zigzag[i]] * divisors[Zigzag[i]]);

        var diff = q[0] - lastDc;
        o.Code(dc[Magnitude(diff)]);
        o.Value(diff);
        int run = 0;
        for (int i = 1; i < 64; i++)
        {
            if (q[i] == 0) { run++; continue; }
            for (; run >= 16; run -= 16) o.Code(ac[0xF0]); // ZRL
            o.Code(ac[(run << 4) | Magnitude(q[i])]);
            o.Value(q[i]);
            run = 0;
        }
        if (run > 0) o.Code(ac[0x00]); // EOB
        return q[0];
    }

    static int Magnitude(int v) => v == 0 ? 0 : 32 - System.Numerics.BitOperations.LeadingZeroCount((uint)Math.Abs(v));

    sealed class BitWriter(int capacity)
    {
        readonly MemoryStream ms = new(capacity);
        uint acc;
        int n;

        public void Bytes(params byte[] b) => ms.Write(b);

        public void Code((ushort Code, byte Length) c) => Bits(c.Code, c.Length);

        // The magnitude's bits; negative values as v - 1 in that many bits.
        public void Value(int v)
        {
            var m = Magnitude(v);
            if (m > 0) Bits((uint)(v < 0 ? v - 1 : v) & ((1u << m) - 1), m);
        }

        void Bits(uint bits, int count)
        {
            acc = (acc << count) | bits;
            n += count;
            while (n >= 8)
            {
                var b = (byte)(acc >> (n - 8));
                ms.WriteByte(b);
                if (b == 0xFF) ms.WriteByte(0); // byte stuffing
                n -= 8;
            }
        }

        public void Flush()
        {
            if (n > 0) Bits((1u << (8 - n)) - 1, 8 - n); // pad with ones
        }

        public byte[] ToArray() => ms.ToArray();
    }
}
