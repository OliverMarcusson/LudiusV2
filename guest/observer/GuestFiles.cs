using System.Runtime.InteropServices;
using Google.Protobuf;
using Ludius.V1;
using Microsoft.Win32.SafeHandles;

namespace Ludius.Observer;

// A done-check's file, read-only: only in this user's noise folders (Documents, Pictures, Downloads, as the controller's
// guestfile.NOISE_FOLDERS), opened for reading with full sharing (an app may still hold it). Fails closed like the
// PowerShell Direct read it replaces: anything that is not a definite answer throws (unknown). A reparse point
// (symlink, junction, mount point) anywhere from the profile down, or a handle that ends up elsewhere, is unknown; only
// a missing file, itself no link, in an existing folder chain without links reads as absent.
static class GuestFiles
{
    public const int Max = 8 << 20; // the guest channel's frame limit is 16 MB
    public static readonly string[] NoiseFolders = ["Documents", "Pictures", "Downloads"];

    public static FileContent Read(ReadFile r, string profile)
    {
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(profile));
        var path = Path.GetFullPath(r.Path);
        if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new UnauthorizedAccessException("outside the user's profile");
        var parts = path[(root.Length + 1)..].Split(Path.DirectorySeparatorChar);
        if (parts.Length < 2 || !NoiseFolders.Contains(parts[0], StringComparer.OrdinalIgnoreCase) || parts.Any(p => p == ""))
            throw new UnauthorizedAccessException("not in a noise folder");

        // The profile and every folder below it down to the file's: real folders, no links.
        var dir = root;
        foreach (var part in parts[..^1].Prepend(""))
        {
            dir = part == "" ? dir : Path.Combine(dir, part);
            var d = new DirectoryInfo(dir);
            if (IsLink(d)) throw new IOException("a link on the path");
            if (!d.Exists) throw new DirectoryNotFoundException("no such folder");
        }

        var f = new FileInfo(path);
        if (IsLink(f)) throw new IOException("the file is a link");
        if (Directory.Exists(path)) throw new IOException("not a file");
        if (!f.Exists) return new FileContent { Exists = false };

        long max = r.MaxBytes is > 0 and <= Max ? r.MaxBytes : Max;
        using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        if (!string.Equals(FinalPath(s.SafeFileHandle), path, StringComparison.OrdinalIgnoreCase))
            throw new IOException("the opened file is not the requested one");
        if (s.Length > max) throw new IOException("too large");
        var data = new MemoryStream();
        s.CopyTo(data);
        var written = new DateTimeOffset(File.GetLastWriteTimeUtc(path)).ToUnixTimeMilliseconds();
        return new FileContent { Exists = true, WrittenMs = written, Data = ByteString.CopyFrom(data.ToArray()) };
    }

    // A symlink (also dangling), junction or other reparse point. Attributes are the entry's own, not its target's.
    static bool IsLink(FileSystemInfo i)
    {
        if (i.LinkTarget is not null) return true;
        try { return (File.GetAttributes(i.FullName) & FileAttributes.ReparsePoint) != 0; }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException) { return false; }
    }

    // Where the open handle really points, links resolved.
    static string FinalPath(SafeFileHandle h)
    {
        if (!OperatingSystem.IsWindows()) return new FileInfo($"/proc/self/fd/{h.DangerousGetHandle()}").LinkTarget ?? "";
        var buf = new char[32768];
        var n = GetFinalPathNameByHandleW(h, buf, (uint)buf.Length, 0);
        if (n == 0 || n >= buf.Length) throw new IOException("final path unavailable");
        var p = new string(buf, 0, (int)n);
        return p.StartsWith(@"\\?\UNC\", StringComparison.Ordinal) ? p : p.StartsWith(@"\\?\", StringComparison.Ordinal) ? p[4..] : p;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle h, char[] buf, uint len, uint flags);
}
