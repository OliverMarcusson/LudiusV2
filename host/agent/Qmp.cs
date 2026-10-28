using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;

namespace Ludius.HostAgent;

// QEMU Machine Protocol over the VM's Unix socket (/var/run/qemu-server/<vmid>.qmp). One connection per batch of
// commands: QEMU serves one QMP client at a time on that socket and Proxmox (qm, pvestatd) needs it too, so a held
// connection would stall the node's own status queries.
sealed class Qmp(string path)
{
    public static readonly TimeSpan Timeout = TimeSpan.FromSeconds(10);

    public async Task<JsonNode?> Execute(string command, JsonObject? arguments, CancellationToken ct) =>
        (await Run([(command, arguments)], ct))[0];

    // passFd: a file descriptor sent along with the first command (SCM_RIGHTS), as getfd expects.
    public async Task<List<JsonNode?>> Run(IReadOnlyList<(string Command, JsonObject? Arguments)> commands, CancellationToken ct, int passFd = -1)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(Timeout); // a busy socket accepts but never greets: give up rather than queue forever
        using var s = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        try { await s.ConnectAsync(new UnixDomainSocketEndPoint(path), cts.Token); }
        catch (SocketException e) { throw new IOException($"QMP socket {path}: {e.Message} (VM off?)", e); }
        await using var ns = new NetworkStream(s);
        using var reader = new StreamReader(ns, new UTF8Encoding(false));
        var greeting = await Line(reader, cts.Token);
        if (greeting["QMP"] is null) throw new IOException("QMP: no greeting");
        await Send(ns, "qmp_capabilities", null, cts.Token);
        await Reply(reader, cts.Token);
        var results = new List<JsonNode?>();
        foreach (var (command, arguments) in commands)
        {
            if (passFd >= 0) { Unix.SendWithFd(s, Message(command, arguments), passFd); passFd = -1; }
            else await Send(ns, command, arguments, cts.Token);
            results.Add(await Reply(reader, cts.Token));
        }
        return results;
    }

    // The monitor's console capture (Captures): one end of a new socket pair goes to QEMU as a VNC client (getfd +
    // add_client, skipauth), so Proxmox's VNC password and config stay untouched; the QMP connection closes right after.
    // View-only by use, not by QEMU: RfbWriter is what keeps the client from sending input. Linux only (SCM_RIGHTS).
    public async Task<Stream> VncClient(CancellationToken ct)
    {
        const string name = "ludius-monitor"; // a leftover from a failed call is replaced by the next getfd
        var (ours, theirs) = Unix.Pair();
        try
        {
            await Run([("getfd", new JsonObject { ["fdname"] = name }),
                ("add_client", new JsonObject { ["protocol"] = "vnc", ["fdname"] = name, ["skipauth"] = true })], ct, theirs);
            return new NetworkStream(ours, ownsSocket: true);
        }
        catch
        {
            ours.Dispose();
            throw;
        }
        finally { Unix.Close(theirs); } // QEMU holds its own copy now
    }

    static byte[] Message(string command, JsonObject? arguments)
    {
        var o = new JsonObject { ["execute"] = command };
        if (arguments is not null) o["arguments"] = arguments.DeepClone();
        return Encoding.UTF8.GetBytes(o.ToJsonString() + "\n");
    }

    static async Task Send(Stream s, string command, JsonObject? arguments, CancellationToken ct) =>
        await s.WriteAsync(Message(command, arguments), ct);

    // Asynchronous events can arrive between a command and its answer: skip them.
    static async Task<JsonNode?> Reply(StreamReader r, CancellationToken ct)
    {
        while (true)
        {
            var o = await Line(r, ct);
            if (o.ContainsKey("return")) return o["return"];
            if (o["error"] is { } e) throw new InvalidOperationException($"QMP {e["class"]}: {e["desc"]}");
        }
    }

    static async Task<JsonObject> Line(StreamReader r, CancellationToken ct) =>
        (await r.ReadLineAsync(ct) ?? throw new IOException("QMP: connection closed")) is var line
        && JsonNode.Parse(line) is JsonObject o ? o : throw new IOException("QMP: not a JSON object");

    // input-send-event building blocks (QAPI InputEvent).
    public static JsonObject Abs(string axis, int value) =>
        new() { ["type"] = "abs", ["data"] = new JsonObject { ["axis"] = axis, ["value"] = value } };

    public static JsonObject Button(string button, bool down) =>
        new() { ["type"] = "btn", ["data"] = new JsonObject { ["down"] = down, ["button"] = button } };

    public static JsonObject Key(string qcode, bool down) =>
        new() { ["type"] = "key", ["data"] = new JsonObject { ["down"] = down, ["key"] = new JsonObject { ["type"] = "qcode", ["data"] = qcode } } };

    public static JsonObject Events(params JsonObject[] events) => new() { ["events"] = new JsonArray(events) };
}

// libc for what .NET sockets lack: socketpair and sendmsg with SCM_RIGHTS (x86-64/arm64 Linux struct layouts).
static class Unix
{
    [StructLayout(LayoutKind.Sequential)]
    struct IoVec { public nint Base; public nuint Length; }

    [StructLayout(LayoutKind.Sequential)]
    struct MsgHdr { public nint Name; public uint NameLength; public nint Iov; public nuint IovLength; public nint Control; public nuint ControlLength; public int Flags; }

    [DllImport("libc", SetLastError = true)] static extern int socketpair(int domain, int type, int protocol, int[] fds);
    [DllImport("libc", SetLastError = true)] static extern nint sendmsg(nint fd, ref MsgHdr msg, int flags);
    [DllImport("libc", EntryPoint = "close")] static extern int CloseFd(int fd);

    public static (Socket Ours, int Theirs) Pair()
    {
        var fds = new int[2];
        if (socketpair(1 /* AF_UNIX */, 1 | 0x80000 /* SOCK_STREAM | SOCK_CLOEXEC */, 0, fds) != 0)
            throw new IOException($"socketpair: errno {Marshal.GetLastPInvokeError()}");
        return (new Socket(new SafeSocketHandle(fds[0], ownsHandle: true)), fds[1]);
    }

    public static void Close(int fd) => CloseFd(fd);

    public static void SendWithFd(Socket s, byte[] data, int fd)
    {
        var pin = GCHandle.Alloc(data, GCHandleType.Pinned);
        var iov = Marshal.AllocHGlobal(Marshal.SizeOf<IoVec>());
        var control = Marshal.AllocHGlobal(24); // CMSG_SPACE(sizeof(int))
        try
        {
            Marshal.StructureToPtr(new IoVec { Base = pin.AddrOfPinnedObject(), Length = (nuint)data.Length }, iov, false);
            Marshal.WriteInt64(control, 0, 20); // cmsg_len = CMSG_LEN(sizeof(int))
            Marshal.WriteInt32(control, 8, 1); // SOL_SOCKET
            Marshal.WriteInt32(control, 12, 1); // SCM_RIGHTS
            Marshal.WriteInt32(control, 16, fd);
            Marshal.WriteInt32(control, 20, 0);
            var msg = new MsgHdr { Iov = iov, IovLength = 1, Control = control, ControlLength = 24 };
            var sent = sendmsg(s.SafeHandle.DangerousGetHandle(), ref msg, 0x4000 /* MSG_NOSIGNAL */);
            if (sent != data.Length) throw new IOException($"QMP sendmsg: {(sent < 0 ? $"errno {Marshal.GetLastPInvokeError()}" : "short write")}");
        }
        finally
        {
            Marshal.FreeHGlobal(control);
            Marshal.FreeHGlobal(iov);
            pin.Free();
        }
    }
}

// Windows virtual-key codes (what the controller sends, see controller keys.py) to QEMU qcodes, and printable ASCII
// to keys on a US layout: the guests' layout (ux_noise role). Hyper-V's TypeText makes the same assumption.
static class Keys
{
    static readonly Dictionary<uint, string> Qcodes = Build();

    static Dictionary<uint, string> Build()
    {
        var m = new Dictionary<uint, string>
        {
            [0x08] = "backspace", [0x09] = "tab", [0x0D] = "ret", [0x10] = "shift", [0x11] = "ctrl", [0x12] = "alt",
            [0x13] = "pause", [0x14] = "caps_lock", [0x1B] = "esc", [0x20] = "spc", [0x21] = "pgup", [0x22] = "pgdn",
            [0x23] = "end", [0x24] = "home", [0x25] = "left", [0x26] = "up", [0x27] = "right", [0x28] = "down",
            [0x2C] = "print", [0x2D] = "insert", [0x2E] = "delete", [0x5B] = "meta_l", [0x5C] = "meta_r", [0x5D] = "menu",
            [0x6A] = "kp_multiply", [0x6B] = "kp_add", [0x6D] = "kp_subtract", [0x6E] = "kp_decimal", [0x6F] = "kp_divide",
            [0x90] = "num_lock", [0x91] = "scroll_lock", [0xA0] = "shift", [0xA1] = "shift_r", [0xA2] = "ctrl",
            [0xA3] = "ctrl_r", [0xA4] = "alt", [0xA5] = "alt_r", [0xBA] = "semicolon", [0xBB] = "equal", [0xBC] = "comma",
            [0xBD] = "minus", [0xBE] = "dot", [0xBF] = "slash", [0xC0] = "grave_accent", [0xDB] = "bracket_left",
            [0xDC] = "backslash", [0xDD] = "bracket_right", [0xDE] = "apostrophe",
        };
        for (uint c = 'A'; c <= 'Z'; c++) m[c] = ((char)(c + 32)).ToString();
        for (uint c = '0'; c <= '9'; c++) m[c] = ((char)c).ToString();
        for (uint i = 0; i < 12; i++) m[0x70 + i] = $"f{i + 1}";
        for (uint i = 0; i < 10; i++) m[0x60 + i] = $"kp_{i}";
        return m;
    }

    public static string Qcode(uint vk) =>
        Qcodes.TryGetValue(vk, out var q) ? q : throw new ArgumentException($"no key for virtual-key 0x{vk:X2}");

    const string Unshifted = "`1234567890-=[]\\;',./";
    const string Shifted = "~!@#$%^&*()_+{}|:\"<>?";

    // The qcode for one character and whether it needs shift.
    public static (string Qcode, bool Shift) ForChar(char c) => c switch
    {
        '\n' => ("ret", false),
        '\t' => ("tab", false),
        ' ' => ("spc", false),
        >= 'a' and <= 'z' => (c.ToString(), false),
        >= 'A' and <= 'Z' => (char.ToLowerInvariant(c).ToString(), true),
        _ when Unshifted.IndexOf(c) is var i and >= 0 => (Qcode(Oem(Unshifted[i])), false),
        _ when Shifted.IndexOf(c) is var i and >= 0 => (Qcode(Oem(Unshifted[i])), true),
        _ => throw new ArgumentException("only printable ASCII, \\n and \\t can be typed"),
    };

    static uint Oem(char c) => c switch
    {
        >= '0' and <= '9' => c,
        '`' => 0xC0, '-' => 0xBD, '=' => 0xBB, '[' => 0xDB, ']' => 0xDD, '\\' => 0xDC,
        ';' => 0xBA, '\'' => 0xDE, ',' => 0xBC, '.' => 0xBE, '/' => 0xBF,
        _ => throw new ArgumentException($"no key for {c}"),
    };
}
