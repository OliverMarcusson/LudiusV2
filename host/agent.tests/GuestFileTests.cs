using System.Diagnostics;
using Ludius.Observer;
using Ludius.V1;
using Xunit;

namespace Ludius.HostAgent.Tests;

// The observer's read_file: fail-closed like the PowerShell Direct read (guestfile.py), confined to the noise folders,
// and a link anywhere on the way is unknown, never content from elsewhere and never "absent".
public class GuestFileTests : IDisposable
{
    readonly string root = Directory.CreateTempSubdirectory("ludius-gf").FullName;
    readonly string profile, docs, outside;

    public GuestFileTests()
    {
        profile = Directory.CreateDirectory(Path.Combine(root, "aberg")).FullName;
        docs = Directory.CreateDirectory(Path.Combine(profile, "Documents")).FullName;
        Directory.CreateDirectory(Path.Combine(profile, "Pictures"));
        Directory.CreateDirectory(Path.Combine(profile, "Downloads"));
        Directory.CreateDirectory(Path.Combine(profile, "Desktop"));
        File.WriteAllText(Path.Combine(docs, "a.txt"), "hello");
        File.WriteAllText(Path.Combine(profile, "Desktop", "a.txt"), "desk");
        outside = Directory.CreateDirectory(Path.Combine(root, "other", "Documents")).FullName; // another profile
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "secret");
    }

    public void Dispose()
    {
        // Unlink first: a recursive delete must not walk into (or trip over) a link's target.
        foreach (var d in Directory.EnumerateDirectories(root, "*", SearchOption.AllDirectories).Where(d => new DirectoryInfo(d).LinkTarget is not null).ToList())
            Directory.Delete(d);
        Directory.Delete(root, true);
    }

    FileContent Read(string path, uint max = 1000) => GuestFiles.Read(new ReadFile { Path = path, MaxBytes = max }, profile);

    // A directory link: a junction on Windows (what a user can make without privileges), a symlink elsewhere.
    static void DirectoryLink(string link, string target)
    {
        if (!OperatingSystem.IsWindows()) { Directory.CreateSymbolicLink(link, target); return; }
        using var p = Process.Start(new ProcessStartInfo("cmd.exe", ["/c", "mklink", "/J", link, target])
            { RedirectStandardOutput = true, UseShellExecute = false })!;
        p.WaitForExit();
        Assert.Equal(0, p.ExitCode);
    }

    [Fact]
    public void A_file_in_a_noise_folder_is_read_and_a_missing_one_is_absent()
    {
        var got = Read(Path.Combine(docs, "a.txt"));
        Assert.True(got.Exists);
        Assert.Equal("hello", got.Data.ToStringUtf8());
        Assert.True(Math.Abs(got.WrittenMs - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()) < 60_000);
        Assert.False(Read(Path.Combine(docs, "missing.txt")).Exists);
        Assert.False(Read(Path.Combine(profile, "Pictures", "missing.png")).Exists);
        Assert.False(Read(Path.Combine(profile, "Downloads", "missing.pdf")).Exists);

        // An app holding the file open for writing does not make it unreadable.
        using var held = new FileStream(Path.Combine(docs, "a.txt"), FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite);
        Assert.True(Read(Path.Combine(docs, "a.txt")).Exists);
    }

    [Fact]
    public void Anything_not_definite_is_unknown()
    {
        Assert.Throws<DirectoryNotFoundException>(() => Read(Path.Combine(docs, "Nope", "a.txt")));
        Directory.CreateDirectory(Path.Combine(docs, "sub"));
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "sub")));
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "a.txt"), 3));
    }

    [Fact]
    public void Only_the_noise_folders_of_this_profile()
    {
        Assert.Throws<UnauthorizedAccessException>(() => Read(Path.Combine(profile, "Desktop", "a.txt")));
        Assert.Throws<UnauthorizedAccessException>(() => Read(Path.Combine(profile, "a.txt")));
        Assert.Throws<UnauthorizedAccessException>(() => Read(docs));
        Assert.Throws<UnauthorizedAccessException>(() => Read(Path.Combine(docs, "..", "..", "other", "Documents", "secret.txt")));
        Assert.Throws<UnauthorizedAccessException>(() => Read(Path.Combine(outside, "secret.txt")));
        Assert.Throws<UnauthorizedAccessException>(() => Read(profile + "-x" + Path.DirectorySeparatorChar + "Documents" + Path.DirectorySeparatorChar + "a"));
    }

    [Fact]
    public void A_junction_or_directory_link_on_the_path_is_unknown()
    {
        DirectoryLink(Path.Combine(docs, "j"), outside);
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "j", "secret.txt")));
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "j", "missing.txt"))); // never "absent" through a link

        // The noise folder itself a link into another profile.
        Directory.Delete(Path.Combine(profile, "Pictures"));
        DirectoryLink(Path.Combine(profile, "Pictures"), outside);
        Assert.Throws<IOException>(() => Read(Path.Combine(profile, "Pictures", "secret.txt")));
    }

    [Fact]
    public void A_file_symlink_is_unknown_and_a_dangling_one_is_not_absent()
    {
        try { File.CreateSymbolicLink(Path.Combine(docs, "link.txt"), Path.Combine(outside, "secret.txt")); }
        catch (IOException) when (OperatingSystem.IsWindows()) { return; } // needs a privilege here: runs on Linux
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "link.txt")));
        File.CreateSymbolicLink(Path.Combine(docs, "dangling.txt"), Path.Combine(outside, "gone.txt"));
        Assert.Throws<IOException>(() => Read(Path.Combine(docs, "dangling.txt")));
    }
}
