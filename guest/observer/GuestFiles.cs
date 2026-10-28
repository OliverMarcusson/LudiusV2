using Google.Protobuf;
using Ludius.V1;

namespace Ludius.Observer;

// A done-check's file, read-only: only inside this user's profile, opened for reading with full sharing (an app may
// still hold it). Fails closed like the PowerShell Direct read it replaces: a missing folder, a directory, a file over
// the limit or any error throws (unknown); only a missing file in an existing folder is absent.
static class GuestFiles
{
    public const int Max = 8 << 20; // the guest channel's frame limit is 16 MB

    public static FileContent Read(ReadFile r, string profile)
    {
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(profile)) + Path.DirectorySeparatorChar;
        var path = Path.GetFullPath(r.Path);
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new UnauthorizedAccessException("outside the user's profile");
        if (!Directory.Exists(Path.GetDirectoryName(path))) throw new DirectoryNotFoundException("no such folder");
        if (Directory.Exists(path)) throw new IOException("not a file");
        long max = r.MaxBytes is > 0 and <= Max ? r.MaxBytes : Max;
        FileStream f;
        try { f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); }
        catch (FileNotFoundException) { return new FileContent { Exists = false }; }
        using (f)
        {
            if (f.Length > max) throw new IOException("too large");
            var data = new MemoryStream();
            f.CopyTo(data);
            var written = new DateTimeOffset(File.GetLastWriteTimeUtc(path)).ToUnixTimeMilliseconds();
            return new FileContent { Exists = true, WrittenMs = written, Data = ByteString.CopyFrom(data.ToArray()) };
        }
    }
}
