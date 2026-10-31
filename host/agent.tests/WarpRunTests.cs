using System.Globalization;
using System.Text.Json;
using Xunit;

namespace Ludius.HostAgent.Tests;

public class WarpRunTests
{
    static long Ms(string at) => new DateTimeOffset(DateTime.Parse(at, CultureInfo.InvariantCulture)).ToUnixTimeMilliseconds();

    static string Status(long dayStartMs)
    {
        var runs = Directory.CreateTempSubdirectory().FullName;
        try
        {
            Directory.CreateDirectory(Path.Combine(runs, "range"));
            Directory.CreateDirectory(Path.Combine(runs, "days"));
            File.WriteAllLines(Path.Combine(runs, "range", "warp.jsonl"), [
                """{"at":"2026-10-31T01:00:00","event":"clock_set","logical":"2027-02-02T22:00:00+01:00"}""",
                """{"at":"2026-10-31T01:05:00","event":"planned","date":"2027-02-03","items":{"lnord":7}}""",
                """{"at":"2026-10-31T01:06:00","event":"clock_set","logical":"2027-02-03T07:50:00+01:00"}""",
                """{"at":"2026-10-31T01:07:00","event":"day","persona":"lnord","date":"2027-02-03","result":"failed: desktop reserved"}""",
            ]);
            File.WriteAllLines(Path.Combine(runs, "days", "2027-02-03-lnord.jsonl"), [
                $$"""{"event":"day_start","host_ms":{{dayStartMs}},"items":7}""",
                $$"""{"event":"day_end","host_ms":{{dayStartMs + 1000}}}""",
            ]);
            using var doc = JsonDocument.Parse(JsonSerializer.Serialize(ConsoleData.Run(runs, Ms("2026-10-31T02:00:00"))));
            return doc.RootElement.GetProperty("personas")[0].GetProperty("status").GetString()!;
        }
        finally { Directory.Delete(runs, true); }
    }

    [Fact]
    public void A_day_rerun_after_the_warp_gave_up_shows_its_own_result() =>
        Assert.Equal("done", Status(Ms("2026-10-31T01:30:00")));

    [Fact]
    public void The_warp_result_stands_for_the_day_it_played() =>
        Assert.Equal("failed", Status(Ms("2026-10-31T01:06:30")));
}
