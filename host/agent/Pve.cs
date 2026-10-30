using System.Diagnostics;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Ludius.HostAgent;

interface IPveApi
{
    Task<JsonNode?> Call(string method, string path, IReadOnlyDictionary<string, string>? args, CancellationToken ct);

    string? Read(string file);
}

sealed partial class Pvesh(string exe = "pvesh") : IPveApi
{
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
        if (Upid().Match(text) is { Success: true } m && !text.StartsWith('{') && !text.StartsWith('[')) return JsonValue.Create(m.Value);
        return JsonNode.Parse(text);
    }

    public string? Read(string file)
    {
        try { return File.ReadAllText(Path.Combine("/etc/pve", file)); }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException) { return null; }
    }

    [GeneratedRegex(@"UPID:[^\s""]+")]
    private static partial Regex Upid();
}

static class Vsock
{
    const int AF_VSOCK = 40, SOCK_STREAM = 1;

    public static Task<Socket> Connect(uint cid, uint port, CancellationToken ct) => Task.Run(() =>
    {
        int fd = socket(AF_VSOCK, SOCK_STREAM | 0x80000 , 0);
        if (fd < 0) throw new IOException($"vsock socket: errno {Marshal.GetLastPInvokeError()}");
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
