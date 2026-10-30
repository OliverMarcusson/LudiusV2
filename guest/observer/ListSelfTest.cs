#if LIST_SELFTEST
using Ludius.V1;

namespace Ludius.Observer;

static class ListSelfTest
{
    static int failures;

    static void Eq<T>(T want, T got, string what)
    {
        if (Equals(want, got)) return;
        failures++;
        Console.WriteLine($"FAIL {what}: want {want}, got {got}");
    }

    static Rect R(int x, int y, int w, int h) => new() { X = x, Y = y, Width = w, Height = h };

    static List<(string, Rect)> Rows(int firstY, int count) =>
        Enumerable.Range(0, count).Select(i => ("list item", R(568, firstY + 21 * i, 272, 22))).ToList();

    public static int Run()
    {
        var list = R(568, 299, 963, 520);
        var header = ("header", R(568, 299, 272, 25));

        var top = new List<(string, Rect)> { header };
        top.AddRange(Rows(330, 24));
        var content = Snapshotter.ContentViewport(list, top);
        Eq(R(568, 324, 963, 495), content, "content viewport sits below the header");
        Eq(R(568, 330, 272, 22), Snapshotter.VisibleRow(R(568, 330, 272, 22), content!), "a whole row keeps its bounds");
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 813, 272, 22), content!), "Dism, 6 px showing, is not a target");
        Eq(23, Snapshotter.VisibleRows(top, content!), "rows shown at the top, header not counted");
        Eq("items 1–23 shown, list continues", Snapshotter.ListShown(23, 0), "exact ordinal at the top");

        var down = new List<(string, Rect)> { header };
        down.AddRange(Rows(314, 25));
        var boot = Snapshotter.VisibleRow(R(568, 314, 272, 22), content!);
        Eq(R(568, 324, 272, 12), boot, "Boot clipped to the part below the header");
        Eq(true, boot.Y + boot.Height / 2 >= 324, "Boot's centre is below the header, not on it");
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 818, 272, 22), content!), "en-US, 1 px showing, is not a target");
        Eq(24, Snapshotter.VisibleRows(down, content!), "rows shown after scrolling");
        Eq("24 items shown, list continues", Snapshotter.ListShown(24, 0.2), "no invented ordinal off the top");

        Eq("23 items shown, list continues", Snapshotter.ListShown(23, 22.5), "off the top, far down");
        Eq("24 items shown, list continues", Snapshotter.ListShown(24, double.NaN), "unknown percent does not claim the top");
        Eq("24 items shown, list continues", Snapshotter.ListShown(24, -1), "negative percent does not claim the top");

        var back = new List<(string, Rect)> { header };
        back.AddRange(Rows(303, 25));
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 303, 272, 22), content!), "sl-SI under the header is not a target");
        Eq(R(568, 807, 272, 12), Snapshotter.VisibleRow(R(568, 807, 272, 22), content!), "WinBioPlugIns clipped at the bottom");
        Eq(24, Snapshotter.VisibleRows(back, content!), "rows shown after scrolling back");

        Eq(list, Snapshotter.ContentViewport(list, Rows(330, 24)), "no header, whole list");
        Eq(null, Snapshotter.ContentViewport(new Rect(), top), "unknown bounds, unknown viewport");
        Eq("list continues", Snapshotter.ListShown(0, 0), "no rows identified, no count");

        Console.WriteLine(failures == 0 ? "list selftest: pass" : $"list selftest: {failures} failed");
        return failures == 0 ? 0 : 1;
    }
}
#endif
