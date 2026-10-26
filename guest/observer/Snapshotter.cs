using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Interop.UIAutomationClient;
using Ludius.V1;
using static Interop.UIAutomationClient.UIA_PropertyIds;

namespace Ludius.Observer;

// Turns the foreground window, its owned and popup windows and the shell into
// one capped element table. Read-only: nothing here invokes a pattern.
sealed partial class Snapshotter
{
    const int Cap = 200;
    const int MaxValue = 300;
    const int UIA_E_TIMEOUT = unchecked((int)0x80131505);
    const int UIA_E_ELEMENTNOTAVAILABLE = unchecked((int)0x80040201);

    static readonly int[] Props =
    [
        UIA_ControlTypePropertyId, UIA_LocalizedControlTypePropertyId, UIA_NamePropertyId, UIA_AutomationIdPropertyId,
        UIA_ClassNamePropertyId, UIA_FrameworkIdPropertyId, UIA_BoundingRectanglePropertyId, UIA_HelpTextPropertyId,
        UIA_IsEnabledPropertyId, UIA_HasKeyboardFocusPropertyId, UIA_IsKeyboardFocusablePropertyId, UIA_IsPasswordPropertyId,
        UIA_LabeledByPropertyId, UIA_ValueValuePropertyId, UIA_ValueIsReadOnlyPropertyId, UIA_ToggleToggleStatePropertyId,
        UIA_SelectionItemIsSelectedPropertyId, UIA_RangeValueValuePropertyId, UIA_ExpandCollapseExpandCollapseStatePropertyId,
        UIA_ScrollVerticallyScrollablePropertyId, UIA_ScrollVerticalScrollPercentPropertyId,
        UIA_IsInvokePatternAvailablePropertyId, UIA_IsValuePatternAvailablePropertyId, UIA_IsTogglePatternAvailablePropertyId,
        UIA_IsSelectionItemPatternAvailablePropertyId, UIA_IsExpandCollapsePatternAvailablePropertyId,
        UIA_IsScrollPatternAvailablePropertyId, UIA_IsRangeValuePatternAvailablePropertyId, UIA_IsTextPatternAvailablePropertyId,
        UIA_LegacyIAccessibleNamePropertyId, UIA_LegacyIAccessibleStatePropertyId, UIA_LegacyIAccessibleDefaultActionPropertyId,
        UIA_LegacyIAccessibleValuePropertyId,
    ];

    // UIA_ButtonControlTypeId -> "button", from the interop's own constants.
    static readonly Dictionary<int, string> Roles = typeof(UIA_ControlTypeIds).GetFields()
        .Where(f => f.Name.EndsWith("ControlTypeId"))
        .ToDictionary(f => (int)f.GetValue(null)!, f => Lower(f.Name["UIA_".Length..^"ControlTypeId".Length]));

    static readonly HashSet<string> Interactive =
        ["button", "check box", "combo box", "edit", "hyperlink", "list item", "menu item", "radio button", "slider",
         "spinner", "split button", "tab item", "tree item", "data item", "document", "menu bar", "scroll bar"];
    static readonly HashSet<string> Lists = ["list", "data grid", "tree", "table"];

    [GeneratedRegex(@"\b(delete|remove|discard|send|pay|uninstall|format|don't save|empty|erase)\b", RegexOptions.IgnoreCase)]
    private static partial Regex Destructive();
    [GeneratedRegex(@"^(ok|save|yes|submit|send|apply|open|print|finish|next|continue|install|replace|retry)$", RegexOptions.IgnoreCase)]
    private static partial Regex Submits();
    [GeneratedRegex(@"^(cancel|close|no|don't save|not now|skip)$", RegexOptions.IgnoreCase)]
    private static partial Regex Closes();

    readonly IUIAutomation2 uia;
    readonly IUIAutomationCacheRequest cache;
    readonly bool uiAccess;
    readonly Dictionary<string, uint> ids = [];
    uint nextId = 1;
    ulong seq;

    public Snapshotter(IUIAutomation2 uia, bool uiAccess)
    {
        this.uia = uia;
        this.uiAccess = uiAccess;
        cache = uia.CreateCacheRequest();
        foreach (var p in Props) cache.AddProperty(p);
        cache.TreeScope = TreeScope.TreeScope_Subtree;
        cache.TreeFilter = uia.CreateAndCondition(uia.ControlViewCondition, uia.CreatePropertyCondition(UIA_IsOffscreenPropertyId, false));
    }

    sealed class Raw
    {
        public required Element E;
        public required Window.Types.Kind Kind;
        public bool Actionable;
        public string Context = "";
        public bool Busy;
    }

    public Snapshot Take()
    {
        var snap = new Snapshot
        {
            Seq = ++seq,
            GuestTimeMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ScreenWidth = Native.GetSystemMetrics(0),
            ScreenHeight = Native.GetSystemMetrics(1),
            Dpi = (int)Native.GetDpiForSystem(),
            User = Environment.UserName,
            Locked = Native.SessionLocked() || !Native.InputDesktopIsDefault(),
        };
        var ci = new Native.CURSORINFO { cbSize = Marshal.SizeOf<Native.CURSORINFO>() };
        if (Native.GetCursorInfo(ref ci))
        {
            snap.CursorX = ci.ptScreenPos.X;
            snap.CursorY = ci.ptScreenPos.Y;
            snap.BusyCursor = ci.hCursor == Native.LoadCursor(0, Native.IDC_WAIT) || ci.hCursor == Native.LoadCursor(0, Native.IDC_APPSTARTING);
        }
        if (snap.Locked) return snap;

        foreach (var h in AppWindows()) snap.Apps.Add(DescribeWindow(h, Window.Types.Kind.Unspecified));
        var raws = new List<Raw>();
        var keys = new HashSet<string>();
        foreach (var (hwnd, kind) in CollectWindows())
        {
            var w = DescribeWindow(hwnd, kind);
            snap.Windows.Add(w);
            if (w.Unresponsive || w.ElevatedUnreadable) continue;
            var found = ReadWindow(hwnd, w, (uint)(snap.Windows.Count - 1), keys);
            if (found.Count == 0) w.NoTree = true;
            raws.AddRange(found);
        }
        snap.IndeterminateProgress = raws.Any(r => r.Busy);
        foreach (var r in raws.Where(r => r.Context != "").Take(60)) snap.Context.Add(r.Context);

        // Priority: focused element, then an open modal or popup (and almost
        // nothing else), else actionable foreground elements in reading order, then the shell.
        var actionable = raws.Where(r => r.Actionable).ToList();
        var ordered = new List<Raw>();
        ordered.AddRange(actionable.Where(r => r.E.Focused));
        bool overlay = snap.Windows.Any(w => w.Modal || w.Kind == Window.Types.Kind.Popup);
        if (overlay)
            ordered.AddRange(actionable.Where(r => r.Kind is Window.Types.Kind.Popup or Window.Types.Kind.Owned));
        else
        {
            ordered.AddRange(actionable.Where(r => r.Kind is Window.Types.Kind.Foreground or Window.Types.Kind.Owned));
            ordered.AddRange(actionable.Where(r => r.Kind == Window.Types.Kind.Shell));
        }
        var distinct = ordered.Distinct().ToList();
        snap.DroppedElements = (uint)Math.Max(0, actionable.Count - Math.Min(distinct.Count, Cap));
        foreach (var r in distinct.Take(Cap)) snap.Elements.Add(r.E);
        return snap;
    }

    static List<(IntPtr, Window.Types.Kind)> CollectWindows()
    {
        var fg = Native.GetForegroundWindow();
        Native.GetWindowThreadProcessId(fg, out var fgPid);
        Native.GetWindowRect(fg, out var fgRect);
        var owned = new List<(IntPtr, Window.Types.Kind)>();
        var popups = new List<(IntPtr, Window.Types.Kind)>();
        var shell = new List<(IntPtr, Window.Types.Kind)>();
        Native.EnumWindows((h, _) =>
        {
            if (h == fg || !Native.IsWindowVisible(h) || Native.Cloaked(h)) return true;
            var cls = Native.ClassName(h);
            for (var o = Native.Owner(h); o != 0; o = Native.Owner(o))
                if (o == fg) { owned.Add((h, Window.Types.Kind.Owned)); return true; }
            Native.GetWindowThreadProcessId(h, out var pid);
            bool popup = (Native.GetWindowLongPtr(h, Native.GWL_STYLE) & Native.WS_POPUP) != 0;
            // Shell first: the taskbar, desktop and Start share explorer's pid with popup styles.
            if (cls is "Shell_TrayWnd" or "NotifyIconOverflowWindow" or "Windows.UI.Core.CoreWindow"
                or "TopLevelWindowForOverflowXamlIsland" or "Progman" or "WorkerW")
            {
                // The desktop (icons) only matters when nothing else covers it.
                if (cls is not ("Progman" or "WorkerW") || Native.ClassName(fg) is "Shell_TrayWnd" or "Progman" or "WorkerW" || fg == 0)
                    shell.Add((h, Window.Types.Kind.Shell));
            }
            else if (cls == "#32768" || (pid == fgPid && fg != 0 && popup && Overlaps(h, fgRect)))
                popups.Add((h, Window.Types.Kind.Popup));
            return true;
        }, 0);
        var all = new List<(IntPtr, Window.Types.Kind)>();
        if (fg != 0) all.Add((fg, Window.Types.Kind.Foreground));
        all.AddRange(owned);
        all.AddRange(popups);
        all.AddRange(shell);
        return all;
    }

    // Taskbar-style app windows: visible, uncloaked, unowned, titled, not tool windows or the shell.
    static List<IntPtr> AppWindows()
    {
        var list = new List<IntPtr>();
        Native.EnumWindows((h, _) =>
        {
            if (!Native.IsWindowVisible(h) || Native.Cloaked(h) || Native.Owner(h) != 0) return true;
            if ((Native.GetWindowLongPtr(h, Native.GWL_EXSTYLE) & Native.WS_EX_TOOLWINDOW) != 0) return true;
            if (Native.Title(h) == "") return true;
            if (Native.ClassName(h) is "Shell_TrayWnd" or "Progman" or "WorkerW" or "Windows.UI.Core.CoreWindow"
                or "Shell_SecondaryTrayWnd" or "NotifyIconOverflowWindow") return true;
            list.Add(h);
            return true;
        }, 0);
        return list;
    }

    static bool Overlaps(IntPtr h, Native.RECT a)
    {
        Native.GetWindowRect(h, out var b);
        return b.Left < a.Right && a.Left < b.Right && b.Top < a.Bottom && a.Top < b.Bottom;
    }

    Window DescribeWindow(IntPtr hwnd, Window.Types.Kind kind)
    {
        Native.GetWindowThreadProcessId(hwnd, out var pid);
        Native.GetWindowRect(hwnd, out var r);
        var w = new Window
        {
            Handle = hwnd,
            Title = Native.Title(hwnd),
            ClassName = Native.ClassName(hwnd),
            ProcessName = Native.ProcessName(pid),
            Pid = pid,
            Kind = kind,
            Bounds = new Rect { X = r.Left, Y = r.Top, Width = r.Right - r.Left, Height = r.Bottom - r.Top },
            Unresponsive = Native.IsHungAppWindow(hwnd),
        };
        if (kind == Window.Types.Kind.Owned)
            w.Modal = Native.Owner(hwnd) is var o && o != 0 && !Native.IsWindowEnabled(o);
        if (!uiAccess && Native.Elevated(pid)) w.ElevatedUnreadable = true;
        return w;
    }

    List<Raw> ReadWindow(IntPtr hwnd, Window w, uint index, HashSet<string> keys)
    {
        for (int attempt = 0; ; attempt++)
        {
            var found = new List<Raw>();
            try
            {
                var root = uia.ElementFromHandle(hwnd).BuildUpdatedCache(cache);
                w.FrameworkId = Str(root, UIA_FrameworkIdPropertyId);
                var winRect = w.Bounds;
                bool sparseDocument = false;
                Walk(root, "", winRect, w, index, found, keys, ref sparseDocument);
                // Chromium builds its tree lazily once a client asks; give it one more look.
                if (sparseDocument && attempt == 0) { Thread.Sleep(200); continue; }
                return found;
            }
            catch (COMException e) when (e.HResult == UIA_E_TIMEOUT) { w.Unresponsive = true; return found; }
            catch (COMException e) when (e.HResult == UIA_E_ELEMENTNOTAVAILABLE) { return found; }
            // E_INVALIDARG arrives as ArgumentException: the window closed between enumeration and reading.
            catch (ArgumentException) { return found; }
            // E_ACCESSDENIED arrives as UnauthorizedAccessException: a window we may not read
            // (integrity level, a closing window). One such window never fails the snapshot.
            catch (Exception e) when (e is UnauthorizedAccessException or COMException) { w.ElevatedUnreadable = true; return found; }
        }
    }

    void Walk(IUIAutomationElement e, string path, Rect clip, Window w, uint index, List<Raw> found, HashSet<string> keys, ref bool sparseDocument)
    {
        var kids = e.GetCachedChildren();
        int n = kids?.Length ?? 0;
        string lastText = "";
        for (int i = 0; i < n; i++)
        {
            var c = kids!.GetElement(i);
            var role = Role(c);
            var raw = Describe(c, role, path, clip, w, index, lastText, i, keys);
            var childClip = clip;
            if (role == "document" && Str(c, UIA_FrameworkIdPropertyId) == "Chrome")
            {
                childClip = raw.E.Bounds; // off-screen nodes are exposed: clip to the viewport
                if ((c.GetCachedChildren()?.Length ?? 0) == 0) sparseDocument = true;
            }
            if (raw.E.Bounds.Width > 0 || raw.E.Focused) found.Add(raw);
            if (role == "text" && raw.E.Name != "") lastText = raw.E.Name;
            var seg = $"{role}:{(raw.E.AutomationId != "" ? raw.E.AutomationId : raw.E.ClassName)}";
            Walk(c, path == "" ? seg : $"{path}/{seg}", childClip, w, index, found, keys, ref sparseDocument);
        }
    }

    Raw Describe(IUIAutomationElement c, string role, string path, Rect clip, Window w, uint index, string lastText, int sibling, HashSet<string> keys)
    {
        var e = new Element
        {
            Role = role,
            AutomationId = Str(c, UIA_AutomationIdPropertyId),
            ClassName = Str(c, UIA_ClassNamePropertyId),
            FrameworkId = Str(c, UIA_FrameworkIdPropertyId),
            AncestorPath = path,
            Enabled = Bool(c, UIA_IsEnabledPropertyId),
            Focused = Bool(c, UIA_HasKeyboardFocusPropertyId),
            KeyboardFocusable = Bool(c, UIA_IsKeyboardFocusablePropertyId),
            Password = Bool(c, UIA_IsPasswordPropertyId),
            Window = index,
            Bounds = Clip(Bounds(c), clip),
        };
        e.Name = NameOf(c, role, lastText);

        if (Bool(c, UIA_IsInvokePatternAvailablePropertyId) || Str(c, UIA_LegacyIAccessibleDefaultActionPropertyId) != "") e.Actions.Add("invoke");
        if (Bool(c, UIA_IsValuePatternAvailablePropertyId))
        {
            e.ReadOnly = Bool(c, UIA_ValueIsReadOnlyPropertyId);
            if (!e.ReadOnly) e.Actions.Add("value");
        }
        if (Bool(c, UIA_IsTogglePatternAvailablePropertyId)) e.Actions.Add("toggle");
        if (Bool(c, UIA_IsSelectionItemPatternAvailablePropertyId)) e.Actions.Add("select");
        if (Bool(c, UIA_IsExpandCollapsePatternAvailablePropertyId)) e.Actions.Add("expand");
        if (Bool(c, UIA_IsScrollPatternAvailablePropertyId)) e.Actions.Add("scroll");

        e.Value = ValueOf(c, e);
        e.Selected = Bool(c, UIA_SelectionItemIsSelectedPropertyId);
        e.Expanded = Int(c, UIA_ExpandCollapseExpandCollapseStatePropertyId) is 1 or 2; // Expanded, PartiallyExpanded

        var key = Hash($"{role}|{e.AutomationId}|{e.Name}|{path}");
        if (!keys.Add(key)) { key = Hash($"{key}#{sibling}"); keys.Add(key); }
        e.Key = key;
        if (!ids.TryGetValue(key, out var id)) ids[key] = id = nextId++;
        e.Id = id;

        Hints(c, e, w);
        bool actionable = e.Actions.Count > 0 || (e.KeyboardFocusable && Interactive.Contains(role));
        if (role is "list" or "tree" or "data grid") actionable = e.Hints.Count > 0 || e.Focused; // containers matter only when they scroll
        var raw = new Raw { E = e, Kind = w.Kind, Actionable = actionable && e.Enabled };
        if (!raw.Actionable && e.Name != "" && role is "text" or "status bar" or "header" or "title bar" or "group")
            raw.Context = $"{role}: {Trim(e.Name, 160)}";
        raw.Busy = role == "progress bar" && !Bool(c, UIA_IsRangeValuePatternAvailablePropertyId);
        return raw;
    }

    // Name -> LabeledBy.Name -> LegacyIAccessible.Name -> preceding text sibling -> HelpText.
    static string NameOf(IUIAutomationElement c, string role, string lastText)
    {
        var name = Str(c, UIA_NamePropertyId);
        if (name != "") return Trim(name, 200);
        try { if (c.CachedLabeledBy is { } l && l.CurrentName is { Length: > 0 } ln) return Trim(ln, 200); } catch (COMException) { }
        name = Str(c, UIA_LegacyIAccessibleNamePropertyId);
        if (name != "") return Trim(name, 200);
        if (role is "edit" or "combo box" or "spinner" && lastText != "") return Trim(lastText, 200);
        return Trim(Str(c, UIA_HelpTextPropertyId), 200);
    }

    static string ValueOf(IUIAutomationElement c, Element e)
    {
        var v = Str(c, UIA_ValueValuePropertyId);
        if (e.Password) return v != "" || Str(c, UIA_LegacyIAccessibleValuePropertyId) != "" ? "filled" : "empty";
        if (v != "") return Trim(v, MaxValue);
        if (Bool(c, UIA_IsTogglePatternAvailablePropertyId))
            return Int(c, UIA_ToggleToggleStatePropertyId) switch { 0 => "off", 1 => "on", _ => "indeterminate" };
        if (Bool(c, UIA_IsRangeValuePatternAvailablePropertyId) && c.GetCachedPropertyValue(UIA_RangeValueValuePropertyId) is double d)
            return d.ToString("0.##");
        if (Bool(c, UIA_IsExpandCollapsePatternAvailablePropertyId))
            return Int(c, UIA_ExpandCollapseExpandCollapseStatePropertyId) is 1 or 2 ? "expanded" : "collapsed";
        return "";
    }

    static void Hints(IUIAutomationElement c, Element e, Window w)
    {
        var label = e.Name.Replace("&", "").Trim();
        if (e.Actions.Contains("expand") && e.Role != "tree item") e.Hints.Add("opens list/menu");
        if (e.Role == "hyperlink") e.Hints.Add(e.Value != "" ? $"link to {Trim(e.Value, 120)}" : "link");
        bool dialog = w.Kind == Window.Types.Kind.Owned || w.ClassName == "#32770";
        if (dialog && e.Role is "button" or "split button")
        {
            if (Submits().IsMatch(label)) e.Hints.Add("submits");
            else if (Closes().IsMatch(label)) e.Hints.Add("closes dialog");
        }
        if (Destructive().IsMatch(label)) e.Hints.Add("destructive");
        if ((Int(c, UIA_LegacyIAccessibleStatePropertyId) & 0x100) != 0) e.Hints.Add("Enter triggers this"); // STATE_SYSTEM_DEFAULT
        if (Lists.Contains(e.Role) && Bool(c, UIA_ScrollVerticallyScrollablePropertyId) &&
            c.GetCachedPropertyValue(UIA_ScrollVerticalScrollPercentPropertyId) is double pct && pct < 99.5)
            e.Hints.Add($"list continues, {c.GetCachedChildren()?.Length ?? 0} visible");
    }

    static Rect Bounds(IUIAutomationElement c) =>
        c.GetCachedPropertyValue(UIA_BoundingRectanglePropertyId) is double[] { Length: 4 } r
            ? new Rect { X = (int)r[0], Y = (int)r[1], Width = (int)r[2], Height = (int)r[3] }
            : new Rect();

    static Rect Clip(Rect a, Rect b)
    {
        int x1 = Math.Max(a.X, b.X), y1 = Math.Max(a.Y, b.Y);
        int x2 = Math.Min(a.X + a.Width, b.X + b.Width), y2 = Math.Min(a.Y + a.Height, b.Y + b.Height);
        return x2 > x1 && y2 > y1 ? new Rect { X = x1, Y = y1, Width = x2 - x1, Height = y2 - y1 } : new Rect();
    }

    static string Role(IUIAutomationElement c) =>
        Roles.TryGetValue(Int(c, UIA_ControlTypePropertyId), out var r) ? r : Str(c, UIA_LocalizedControlTypePropertyId);

    static string Str(IUIAutomationElement c, int id) => c.GetCachedPropertyValue(id) as string ?? "";
    static bool Bool(IUIAutomationElement c, int id) => c.GetCachedPropertyValue(id) is true;
    static int Int(IUIAutomationElement c, int id) => c.GetCachedPropertyValue(id) switch { int i => i, uint u => unchecked((int)u), _ => 0 };
    static string Trim(string s, int max) => s.Length <= max ? s : s[..max] + "…";
    static string Hash(string s) => Convert.ToHexString(SHA1.HashData(Encoding.UTF8.GetBytes(s)), 0, 8).ToLowerInvariant();

    // "SplitButton" -> "split button", "DataItem" -> "data item"
    static string Lower(string pascal) => Regex.Replace(pascal, "(?<=[a-z])([A-Z])", " $1").ToLowerInvariant();
}
