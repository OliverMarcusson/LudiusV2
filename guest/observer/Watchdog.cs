namespace Ludius.Observer;

// Spec: restarted by watchdog to contain UIA client leaks. A request that hangs past the UIA timeouts, or a run
// of failed snapshots, is reported as a fault; the caller then crashes the process so the scheduled task's
// crash trigger starts a fresh observer. Idle() lets a daily takeover wait for a gap between requests.
sealed class Watchdog(Func<long> nowMs, long hungMs = 60_000, int maxFailures = 5)
{
    long busySince = -1; // start of the request in progress, -1 between requests
    long lastActivity = nowMs();
    int failures;
    // What the observer is doing (a window being read, settle tracking): a hang's fault names it.
    public static volatile string Step = "start";

    public void Begin() => Volatile.Write(ref busySince, nowMs());

    public void End(bool ok)
    {
        Volatile.Write(ref failures, ok ? 0 : failures + 1);
        Volatile.Write(ref lastActivity, nowMs());
        Volatile.Write(ref busySince, -1);
    }

    public string? Fault()
    {
        var since = Volatile.Read(ref busySince);
        if (since >= 0 && nowMs() - since > hungMs) return $"request hung for {(nowMs() - since) / 1000} s during {Step}";
        var n = Volatile.Read(ref failures);
        return n >= maxFailures ? $"{n} requests failed in a row" : null;
    }

    public bool Idle(long quietMs) =>
        Volatile.Read(ref busySince) < 0 && nowMs() - Volatile.Read(ref lastActivity) >= quietMs;
}
