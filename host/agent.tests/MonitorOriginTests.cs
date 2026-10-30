using Xunit;
using MonitorRoutes = global::Monitor;

namespace Ludius.HostAgent.Tests;

public class MonitorOriginTests
{
    const string Local = "127.0.0.1:7452";
    static readonly IReadOnlySet<string> None = MonitorRoutes.ParseOrigins(null);
    static readonly IReadOnlySet<string> Tunnel = MonitorRoutes.ParseOrigins("https://monitor.ludius.se");

    [Fact]
    public void The_configured_origin_is_accepted()
    {
        Assert.True(MonitorRoutes.OriginAllowed("https://monitor.ludius.se", Local, Tunnel));
        Assert.False(MonitorRoutes.OriginAllowed("https://monitor.ludius.se", Local, None));
    }

    [Fact]
    public void Other_origins_and_a_missing_origin_behave_as_today()
    {
        foreach (var allowed in new[] { None, Tunnel })
        {
            Assert.True(MonitorRoutes.OriginAllowed("", Local, allowed));
            Assert.True(MonitorRoutes.OriginAllowed("http://127.0.0.1:7452", Local, allowed));
            foreach (var other in new[]
            {
                "https://evil.example", "https://monitor.ludius.se.evil.example", "http://monitor.ludius.se",
                "https://monitor.ludius.se:8443", "https://sub.monitor.ludius.se", "null", "not a url", "http://127.0.0.1:7451",
            })
                Assert.False(MonitorRoutes.OriginAllowed(other, Local, allowed), other);
        }
    }

    [Fact]
    public void Only_bare_http_origins_can_be_configured()
    {
        Assert.Empty(MonitorRoutes.ParseOrigins(""));
        Assert.Equal(["https://monitor.ludius.se", "https://other.example:8443"],
            MonitorRoutes.ParseOrigins(" https://Monitor.Ludius.se ; https://other.example:8443;").Order());
        Assert.Equal(["https://monitor.ludius.se"], MonitorRoutes.ParseOrigins("https://monitor.ludius.se:443"));
        foreach (var bad in new[] { "monitor.ludius.se", "*", "https://monitor.ludius.se/", "https://monitor.ludius.se/x",
                                    "https://monitor.ludius.se?a=1", "ftp://monitor.ludius.se", "https://u@monitor.ludius.se" })
            Assert.Throws<InvalidOperationException>(() => MonitorRoutes.ParseOrigins(bad));
    }
}
