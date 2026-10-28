using System.Text.RegularExpressions;

namespace Ludius.HostAgent;

// What the gRPC service, the monitor and the feed need from a hypervisor (spec section 4). Hyper-V and Proxmox
// implement it; nothing above this line knows which one runs.
interface IHypervisor
{
    VmHandle Get(string name);
    Task Move(VmHandle vm, int x, int y, uint durationMs, CancellationToken ct);
    Task Click(VmHandle vm, int button, uint count, CancellationToken ct); // 1 left, 2 right, 3 middle
    void Key(VmHandle vm, uint vk, bool down);
    Task Chord(VmHandle vm, IReadOnlyList<uint> keys, CancellationToken ct);
    Task Type(VmHandle vm, string text, uint meanMs, uint jitterMs, CancellationToken ct);
    void CtrlAltDel(VmHandle vm);
    Task Scroll(VmHandle vm, int notches, CancellationToken ct);
    Task<string> Lifecycle(string vm, Ludius.V1.LifecycleRequest.Types.Op op, string checkpoint, string target, CancellationToken ct);
    (byte[] Png, int Width, int Height) Screenshot(VmHandle vm);
    List<VmInfo> ListVms();
    byte[] Frame(string name, int maxWidth); // view only: a JPEG or PNG the monitor can decode
}

class VmHandle
{
    public required string Name;
    public required GuestChannel Channel;
    public readonly SemaphoreSlim InputGate = new(1, 1);
    public (int X, int Y)? Pointer;
    public (int W, int H)? Screen; // guest resolution as the observer last reported it
}

public sealed record VmInfo(string Name, string State, int Width, int Height);

static partial class VmName
{
    [GeneratedRegex(@"^[\w .-]{1,64}$")]
    private static partial Regex Safe();

    public static bool Valid(string name) => Safe().IsMatch(name);
}

// Shared by both backends so they move and type alike.
static class Human
{
    // A slightly curved, eased path rather than a teleport or a ruler line. Stays inside the box spanned by start
    // and target and never at 0: Hyper-V rejects off-screen points (32773) and ignores (0, 0).
    public static IEnumerable<(int X, int Y)> Path((int X, int Y)? from, int x, int y, int steps)
    {
        double bend = (Random.Shared.NextDouble() - 0.5) * 0.25;
        for (int i = 1; i <= steps; i++)
        {
            double t = (double)i / steps, e = t * t * (3 - 2 * t);
            int px = x, py = y;
            if (from is { } f && i < steps)
            {
                double dx = x - f.X, dy = y - f.Y;
                double arc = Math.Sin(Math.PI * t) * bend;
                px = Math.Max(1, Math.Clamp((int)Math.Round(f.X + dx * e - dy * arc), Math.Min(f.X, x), Math.Max(f.X, x)));
                py = Math.Max(1, Math.Clamp((int)Math.Round(f.Y + dy * e + dx * arc), Math.Min(f.Y, y), Math.Max(f.Y, y)));
            }
            yield return (px, py);
        }
    }

    public static int Steps((int X, int Y)? from, uint durationMs) =>
        from is null || durationMs < 30 ? 1 : (int)Math.Clamp(durationMs / 15, 2, 60);

    // Heavy-ish tail: mostly near the mean, sometimes a pause.
    public static int KeyDelay(uint meanMs, uint jitterMs)
    {
        double delay = meanMs + (Random.Shared.NextDouble() * 2 - 1) * jitterMs;
        if (Random.Shared.NextDouble() < 0.04) delay += meanMs * (2 + Random.Shared.NextDouble() * 4);
        return Math.Max(0, (int)delay);
    }
}
