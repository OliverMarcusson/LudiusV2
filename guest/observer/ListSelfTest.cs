#if LIST_SELFTEST
using Ludius.V1;

namespace Ludius.Observer;

// Regression check for list clipping and the "list continues" hint, on the geometry Waffle measured live in
// Explorer's System32 details view (runs/frameworks/20261027-113344-explorer-list-live.txt). Pure seams only:
// no UIA. Built with -p:ListSelfTest=true, run with redirected stdout; exit code 0 = pass.
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

    // Rows are 22 px on a 21 px pitch. Row x and width were not recorded; the list's x and the header's width stand in.
    static List<(string, Rect)> Rows(int firstY, int count) =>
        Enumerable.Range(0, count).Select(i => ("list item", R(568, firstY + 21 * i, 272, 22))).ToList();

    public static int Run()
    {
        var list = R(568, 299, 963, 520);
        var header = ("header", R(568, 299, 272, 25));

        // Initial (top): 0409 at y 330 .. Dism at y 813, 6 px inside the list.
        var top = new List<(string, Rect)> { header };
        top.AddRange(Rows(330, 24));
        var content = Snapshotter.ContentViewport(list, top);
        Eq(R(568, 324, 963, 495), content, "content viewport sits below the header");
        Eq(R(568, 330, 272, 22), Snapshotter.VisibleRow(R(568, 330, 272, 22), content!), "a whole row keeps its bounds");
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 813, 272, 22), content!), "Dism, 6 px showing, is not a target");
        Eq(23, Snapshotter.VisibleRows(top, content!), "rows shown at the top, header not counted");
        Eq("items 1–23 shown, list continues", Snapshotter.ListShown(23, 0, 0), "exact ordinal at the top");

        // -3 notches: Boot at y 314 (12 px below the header) .. en-US at y 818 (1 px).
        var down = new List<(string, Rect)> { header };
        down.AddRange(Rows(314, 25));
        var boot = Snapshotter.VisibleRow(R(568, 314, 272, 22), content!);
        Eq(R(568, 324, 272, 12), boot, "Boot clipped to the part below the header");
        Eq(true, boot.Y + boot.Height / 2 >= 324, "Boot's centre is below the header, not on it");
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 818, 272, 22), content!), "en-US, 1 px showing, is not a target");
        Eq(24, Snapshotter.VisibleRows(down, content!), "rows shown after scrolling");
        // ~0.2 % down: not the top, so no "items 1–N". Explorer reported no row count here.
        Eq("24 items shown, list continues", Snapshotter.ListShown(24, 0.2, 0), "no invented ordinal off the top");
        Eq("about items 10–33 shown of 4501 (estimated from scroll position), list continues",
            Snapshotter.ListShown(24, 0.2, 4501), "an estimate says so");

        // +300 back: sl-SI at y 303 (1 px below the header) .. WinBioPlugIns at y 807 (12 px).
        var back = new List<(string, Rect)> { header };
        back.AddRange(Rows(303, 25));
        Eq(new Rect(), Snapshotter.VisibleRow(R(568, 303, 272, 22), content!), "sl-SI under the header is not a target");
        Eq(R(568, 807, 272, 12), Snapshotter.VisibleRow(R(568, 807, 272, 22), content!), "WinBioPlugIns clipped at the bottom");
        Eq(24, Snapshotter.VisibleRows(back, content!), "rows shown after scrolling back");

        // No header: the whole list is the viewport. Unknown list bounds: no viewport, no clipping, no count.
        Eq(list, Snapshotter.ContentViewport(list, Rows(330, 24)), "no header, whole list");
        Eq(null, Snapshotter.ContentViewport(new Rect(), top), "unknown bounds, unknown viewport");
        Eq("list continues", Snapshotter.ListShown(0, 0, 0), "no rows identified, no count");
        Eq("items 1–23 shown of 4501, list continues", Snapshotter.ListShown(23, 0, 4501), "top with a row count");

        Console.WriteLine(failures == 0 ? "list selftest: pass" : $"list selftest: {failures} failed");
        return failures == 0 ? 0 : 1;
    }
}
#endif
