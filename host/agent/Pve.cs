using System.Diagnostics;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Ludius.HostAgent;

// The Proxmox API, node-local. Faked in the tests.
interface IPveApi
{
    // method: get, create (POST), set (PUT), delete. Returns the API's data.
    Task<JsonNode?> Call(string method, string path, IReadOnlyDictionary<string, string>? args, CancellationToken ct);
}

// The API through pvesh, as root on the node the agent runs on: no API token to keep, no TLS to pin, and root@pam may
// set a VM's `args` (the vsock device), which an API token may not.
sealed partial class Pvesh(string exe = "pvesh") : IPveApi
{
    // No shell: every value is its own argv entry, so spaces, quotes or `$(...)` in a value stay literal.
    public static ProcessStartInfo StartInfo(string exe, string method, string path, IReadOnlyDictionary<string, string>? args)
    {
        var psi = new ProcessStartInfo(exe) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        psi.ArgumentList.Add(method);
        psi.ArgumentList.Add(path);
        psi.ArgumentList.Add("--output-format");
        psi.ArgumentList.Add("json");
        foreach (var (k, v) in args ?? new Dictionary<string, string>())
        {
            psi.ArgumentList.Add("--" + k);
            psi.ArgumentList.Add(v);
        }
        return psi;
    }

    public async Task<JsonNode?> Call(string method, string path, IReadOnlyDictionary<string, string>? args, CancellationToken ct)
    {
        using var p = Process.Start(StartInfo(exe, method, path, args))!;
        var stdout = p.StandardOutput.ReadToEndAsync(ct);
        var stderr = p.StandardError.ReadToEndAsync(ct);
        await p.WaitForExitAsync(ct);
        if (p.ExitCode != 0) throw new InvalidOperationException($"pvesh {method} {path}: {(await stderr).Trim()}");
        var text = (await stdout).Trim();
        if (text == "") return null;
        // A worker task (start, clone, snapshot ...) may print its log before the UPID: keep the UPID.
        if (Upid().Match(text) is { Success: true } m && !text.StartsWith('{') && !text.StartsWith('[')) return JsonValue.Create(m.Value);
        return JsonNode.Parse(text);
    }

    [GeneratedRegex(@"UPID:[^\s""]+")]
    private static partial Regex Upid();
}

// AF_VSOCK on Linux. .NET's Socket rejects the family ("address family not supported"), so the socket is made and
// connected through libc and only then wrapped, connected, into a Socket. The kernel bounds the connect (2 s default).
static class Vsock
{
    const int AF_VSOCK = 40, SOCK_STREAM = 1;

    public static Task<Socket> Connect(uint cid, uint port, CancellationToken ct) => Task.Run(() =>
    {
        int fd = socket(AF_VSOCK, SOCK_STREAM | 0x80000 /* SOCK_CLOEXEC */, 0);
        if (fd < 0) throw new IOException($"vsock socket: errno {Marshal.GetLastPInvokeError()}");
        // struct sockaddr_vm: family, reserved, port, cid, zero[4]
        var addr = new byte[16];
        BitConverter.TryWriteBytes(addr.AsSpan(0, 2), (ushort)AF_VSOCK);
        BitConverter.TryWriteBytes(addr.AsSpan(4, 4), port);
        BitConverter.TryWriteBytes(addr.AsSpan(8, 4), cid);
        if (connect(fd, addr, addr.Length) != 0)
        {
            var errno = Marshal.GetLastPInvokeError();
            close(fd);
            throw new IOException($"vsock connect to cid {cid} port {port}: errno {errno}");
        }
        return new Socket(new SafeSocketHandle((IntPtr)fd, ownsHandle: true));
    }, ct).WaitAsync(ct);

    [DllImport("libc", SetLastError = true)] static extern int socket(int domain, int type, int protocol);
    [DllImport("libc", SetLastError = true)] static extern int connect(int fd, byte[] addr, int len);
    [DllImport("libc")] static extern int close(int fd);
}
