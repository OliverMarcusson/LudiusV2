using System.Buffers.Binary;
using System.Net.Sockets;
using Google.Protobuf;
using Ludius.Observer;
using Ludius.V1;

namespace Ludius.HostAgent;

// The host end of one VM's observer link: request/response over a Hyper-V
// socket, reconnecting after drops (observer restarts, VM restores).
sealed class GuestChannel(Guid vmId, Guid service) : IDisposable
{
    readonly SemaphoreSlim gate = new(1, 1);
    Socket? socket;
    NetworkStream? stream;
    ulong nextId;

    public Hello? Hello { get; private set; }

    public async Task<ObserverResponse> Request(ObserverRequest req, TimeSpan timeout, CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            for (int attempt = 0; ; attempt++)
            {
                try
                {
                    await Connect(ct);
                    req.Id = ++nextId;
                    await Write(stream!, req, ct);
                    using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    cts.CancelAfter(timeout);
                    while (true)
                    {
                        var res = ObserverResponse.Parser.ParseFrom(await ReadFrame(stream!, cts.Token));
                        if (res.Id == req.Id) return res;
                        if (res.Hello != null) Hello = res.Hello;
                    }
                }
                catch (Exception e) when (e is SocketException or IOException or OperationCanceledException or InvalidProtocolBufferException && !ct.IsCancellationRequested)
                {
                    Drop();
                    if (attempt >= 1) throw new IOException($"observer unreachable: {e.Message}", e);
                }
            }
        }
        finally { gate.Release(); }
    }

    async Task Connect(CancellationToken ct)
    {
        if (stream != null) return;
        var s = new Socket(HyperVEndPoint.Family, SocketType.Stream, HyperVEndPoint.RawProtocol);
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromSeconds(5));
            // ConnectEx (ConnectAsync) is TCP-only: WSAEINVAL on AF_HYPERV.
            await Task.Run(() => s.Connect(new HyperVEndPoint(vmId, service)), ct).WaitAsync(cts.Token);
            var ns = new NetworkStream(s, ownsSocket: true);
            var hello = ObserverResponse.Parser.ParseFrom(await ReadFrame(ns, cts.Token));
            Hello = hello.Hello ?? throw new IOException("observer did not say hello");
            socket = s;
            stream = ns;
        }
        catch { s.Dispose(); throw; }
    }

    public void Drop()
    {
        stream?.Dispose();
        socket?.Dispose();
        stream = null;
        socket = null;
    }

    public void Dispose() => Drop();

    static async Task Write(Stream s, IMessage m, CancellationToken ct)
    {
        var body = m.ToByteArray();
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(frame, (uint)body.Length);
        body.CopyTo(frame, 4);
        await s.WriteAsync(frame, ct);
    }

    static async Task<byte[]> ReadFrame(Stream s, CancellationToken ct)
    {
        var len = new byte[4];
        await s.ReadExactlyAsync(len, ct);
        var n = BinaryPrimitives.ReadUInt32LittleEndian(len);
        if (n > 16 << 20) throw new IOException("frame too large");
        var body = new byte[n];
        await s.ReadExactlyAsync(body, ct);
        return body;
    }
}
