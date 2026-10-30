namespace Ludius.Observer;

sealed class Watchdog(Func<long> nowMs, long hungMs = 60_000, int maxFailures = 5)
{
    long busySince = -1;
    long lastActivity = nowMs();
    int failures;
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
