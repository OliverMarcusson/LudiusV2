using System.Buffers.Binary;
using System.Net.Sockets;
using Google.Protobuf;
using Interop.UIAutomationClient;
using Ludius.Observer;
using Ludius.V1;

// Read-only UIA observer for one interactive session. Started at logon by a
// scheduled task; serves one host connection at a time over a Hyper-V socket.
// Writes nothing to disk. `--once` prints one settled snapshot as JSON (dev only).
const string Version = "0.1.0";
var service = Guid.Parse(Arg("--service") ?? "000011cb-facb-11e6-bd58-64006a7986d3");

// Only in a user's interactive session: session 0 cannot see the desktop.
Native.ProcessIdToSessionId((uint)Environment.ProcessId, out var ownSession);
if (ownSession == 0 && !args.Contains("--once")) return 2;

// One observer per session: a new instance (daily restart) asks the running one to quit and takes over. The
// running one finishes its request and waits for a quiet link first, so the swap falls between tasks when it can.
var dog = new Watchdog(() => Environment.TickCount64);
using var quit = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\RangeAssistQuit");
using var single = new Mutex(false, @"Local\RangeAssist");
if (!args.Contains("--once"))
{
    quit.Set();
    try { if (!single.WaitOne(TimeSpan.FromSeconds(150))) return 1; } catch (AbandonedMutexException) { }
    quit.Reset();
    new Thread(() =>
    {
        quit.WaitOne();
        for (var waited = 0; waited < 120_000 && !dog.Idle(10_000); waited += 250) Thread.Sleep(250);
        Environment.Exit(0);
    }) { IsBackground = true }.Start();
    // A hang or a run of failures becomes a crash; the task's crash trigger starts a fresh observer.
    new Thread(() =>
    {
        while (true)
        {
            Thread.Sleep(1000);
            if (dog.Fault() is { } fault) Environment.FailFast("observer watchdog: " + fault);
        }
    }) { IsBackground = true }.Start();
}

var uia = (IUIAutomation2)new CUIAutomation8();
uia.ConnectionTimeout = 2000;  // a hung app becomes an unresponsive window,
uia.TransactionTimeout = 3000; // not a frozen observer
bool uiAccess = Native.SelfUiAccess();
var settle = new Settle(uia);
var snapper = new Snapshotter(uia, uiAccess);

if (args.Contains("--once"))
{
    Console.OutputEncoding = System.Text.Encoding.UTF8;
    Console.WriteLine(JsonFormatter.Default.Format(Observe(new SnapshotRequest { WaitSettle = true })));
    return 0;
}

using var listener = new Socket(HyperVEndPoint.Family, SocketType.Stream, HyperVEndPoint.RawProtocol);
listener.Bind(new HyperVEndPoint(Guid.Empty, service)); // HV_GUID_WILDCARD
listener.Listen(1);
while (true)
{
    using var conn = listener.Accept();
    try { Serve(conn); }
    catch (Exception e) when (e is SocketException or IOException or EndOfStreamException) { }
}

void Serve(Socket conn)
{
    using var stream = new NetworkStream(conn);
    Send(stream, new ObserverResponse
    {
        Hello = new Hello
        {
            Version = Version, User = Environment.UserName, SessionId = ownSession, UiAccess = uiAccess,
            PixelSettle = settle.PixelSettle, Computer = Environment.MachineName,
        },
    });
    while (true)
    {
        var req = ObserverRequest.Parser.ParseFrom(Read(stream));
        var res = new ObserverResponse { Id = req.Id };
        dog.Begin();
        try
        {
            switch (req.BodyCase)
            {
                case ObserverRequest.BodyOneofCase.Snapshot: res.Snapshot = Observe(req.Snapshot); break;
                case ObserverRequest.BodyOneofCase.Ping:
                    res.Hello = new Hello { Version = Version, User = Environment.UserName, UiAccess = uiAccess, PixelSettle = settle.PixelSettle };
                    break;
                default: res.Error = "unknown request"; break;
            }
            dog.End(ok: true);
        }
        catch (Exception e)
        {
            res.Error = e.GetType().Name + ": " + e.Message;
            dog.End(ok: false);
        }
        Send(stream, res);
    }
}

Snapshot Observe(SnapshotRequest r)
{
    var (settled, ms) = r.WaitSettle
        ? settle.Wait(r.QuietMs == 0 ? 300 : r.QuietMs, r.TimeoutMs == 0 ? 5000 : r.TimeoutMs)
        : (true, 0u);
    var snap = snapper.Take();
    snap.Settled = settled;
    snap.SettleMs = ms;
    return snap;
}

// Frames: u32 little-endian length, then the protobuf message.
static void Send(Stream s, IMessage m)
{
    var body = m.ToByteArray();
    Span<byte> len = stackalloc byte[4];
    BinaryPrimitives.WriteUInt32LittleEndian(len, (uint)body.Length);
    s.Write(len);
    s.Write(body);
    s.Flush();
}

static byte[] Read(Stream s)
{
    Span<byte> len = stackalloc byte[4];
    s.ReadExactly(len);
    var n = BinaryPrimitives.ReadUInt32LittleEndian(len);
    if (n > 16 << 20) throw new IOException("frame too large");
    var body = new byte[n];
    s.ReadExactly(body);
    return body;
}

string? Arg(string name) => Array.IndexOf(args, name) is var i and >= 0 && i + 1 < args.Length ? args[i + 1] : null;
