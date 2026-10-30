using Xunit;

namespace Ludius.HostAgent.Tests;

public class InteractionLinesTests
{
    [Fact]
    public void Bad_and_unfinished_lines_are_skipped_and_only_the_tail_is_read()
    {
        var path = Path.GetTempFileName();
        try
        {
            Assert.Empty(ConsoleData.LastLines(path + ".missing", 20));
            var filler = string.Concat(Enumerable.Range(0, 2000).Select(i => $"{{\"id\":\"old{i}\",\"pad\":\"{new string('x', 40)}\"}}\n"));
            File.WriteAllText(path, filler + "{\"id\":\"a\"}\nnot json\n[1]\n{\"id\":\"b\"}\n{\"id\":\"half");
            var got = ConsoleData.LastLines(path, 3).Select(o => (string)o["id"]!).ToArray();
            Assert.Equal(["old1999", "a", "b"], got);
            Assert.True(new FileInfo(path).Length > 64 << 10);
            Assert.DoesNotContain(ConsoleData.LastLines(path, 5000), o => (string)o["id"]! == "old0");
        }
        finally { File.Delete(path); }
    }
}
