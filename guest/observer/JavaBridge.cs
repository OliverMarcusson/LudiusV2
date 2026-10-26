using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace Ludius.Observer;

// Java Access Bridge client. Swing and AWT windows show UIA almost nothing; the bridge exposes their accessibility
// tree. The JRE's WindowsAccessBridge-64.dll loads into this process and talks to each JVM over window messages and
// shared memory: no child process, no disk, no network. Its handshake needs a message pump on the thread that called
// Windows_run, so one background thread owns that. No JRE on the machine means no Java support, never a failure.
static unsafe class JavaBridge
{
    public const int MaxString = 1024, ShortString = 256, MaxVisibleChildren = 256;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct ContextInfo
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = MaxString)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = MaxString)] public string Description;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = ShortString)] public string Role;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = ShortString)] public string RoleEnUs;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = ShortString)] public string States;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = ShortString)] public string StatesEnUs;
        public int IndexInParent, ChildrenCount, X, Y, Width, Height;
        public int AccessibleComponent, AccessibleAction, AccessibleSelection, AccessibleText, AccessibleInterfaces;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct TextInfo { public int CharCount, CaretIndex, IndexAtPoint; }

    [StructLayout(LayoutKind.Sequential)]
    struct VisibleChildren
    {
        public int Count;
        public fixed long Children[MaxVisibleChildren];
    }

    [StructLayout(LayoutKind.Sequential)]
    struct MSG { public IntPtr Hwnd; public uint Message; public IntPtr WParam, LParam; public uint Time; public int X, Y, Private; }

    [DllImport("user32.dll")] static extern int GetMessageW(out MSG msg, IntPtr hwnd, uint min, uint max);
    [DllImport("user32.dll")] static extern int TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessageW(ref MSG msg);

    static delegate* unmanaged<void> windowsRun;
    static delegate* unmanaged<IntPtr, int> isJavaWindow;
    static delegate* unmanaged<IntPtr, int*, long*, int> contextFromHwnd;
    static delegate* unmanaged<int, long, void*, int> contextInfo;
    static delegate* unmanaged<int, long, int, long> childAt;
    static delegate* unmanaged<int, long, int, VisibleChildren*, int> visibleChildren;
    static delegate* unmanaged<int, long, TextInfo*, int, int, int> textInfo;
    static delegate* unmanaged<int, long, int, int, char*, short, int> textRange;
    static delegate* unmanaged<int, long, void> release;

    public static bool Ready { get; private set; }

    public static void Start()
    {
        var dll = Load();
        if (dll == 0) return;
        try
        {
            windowsRun = (delegate* unmanaged<void>)NativeLibrary.GetExport(dll, "Windows_run");
            isJavaWindow = (delegate* unmanaged<IntPtr, int>)NativeLibrary.GetExport(dll, "isJavaWindow");
            contextFromHwnd = (delegate* unmanaged<IntPtr, int*, long*, int>)NativeLibrary.GetExport(dll, "getAccessibleContextFromHWND");
            contextInfo = (delegate* unmanaged<int, long, void*, int>)NativeLibrary.GetExport(dll, "getAccessibleContextInfo");
            childAt = (delegate* unmanaged<int, long, int, long>)NativeLibrary.GetExport(dll, "getAccessibleChildFromContext");
            visibleChildren = (delegate* unmanaged<int, long, int, VisibleChildren*, int>)NativeLibrary.GetExport(dll, "getVisibleChildren");
            textInfo = (delegate* unmanaged<int, long, TextInfo*, int, int, int>)NativeLibrary.GetExport(dll, "getAccessibleTextInfo");
            textRange = (delegate* unmanaged<int, long, int, int, char*, short, int>)NativeLibrary.GetExport(dll, "getAccessibleTextRange");
            release = (delegate* unmanaged<int, long, void>)NativeLibrary.GetExport(dll, "releaseJavaObject");
        }
        catch (EntryPointNotFoundException) { return; }
        using var started = new ManualResetEventSlim();
        new Thread(() =>
        {
            windowsRun();
            started.Set();
            while (GetMessageW(out var m, 0, 0, 0) > 0) { TranslateMessage(ref m); DispatchMessageW(ref m); }
        }) { IsBackground = true, Name = "jab" }.Start();
        started.Wait();
        Thread.Sleep(1000); // running JVMs answer the bridge's announcement asynchronously
        Ready = true;
    }

    // The bridge DLL ships in the JRE's bin folder; older installers also copied it to System32.
    static IntPtr Load()
    {
        const string name = "WindowsAccessBridge-64.dll";
        var homes = new List<string?> { Environment.GetEnvironmentVariable("JAVA_HOME") };
        foreach (var kind in new[] { "JRE", "JDK" })
        {
            using var k = Registry.LocalMachine.OpenSubKey($@"SOFTWARE\JavaSoft\{kind}");
            if (k?.GetValue("CurrentVersion") is string v)
                using (var cur = k.OpenSubKey(v)) homes.Add(cur?.GetValue("JavaHome") as string);
        }
        foreach (var home in homes)
            if (home is { Length: > 0 } && NativeLibrary.TryLoad(Path.Combine(home, "bin", name), out var h)) return h;
        return NativeLibrary.TryLoad(name, typeof(JavaBridge).Assembly, DllImportSearchPath.System32, out var sys) ? sys : 0;
    }

    public static bool IsJava(IntPtr hwnd) => Ready && isJavaWindow(hwnd) != 0;

    public static bool Root(IntPtr hwnd, out int vm, out long ac)
    {
        int v; long a;
        bool ok = contextFromHwnd(hwnd, &v, &a) != 0;
        (vm, ac) = (v, a);
        return ok;
    }

    public static bool Info(int vm, long ac, out ContextInfo info)
    {
        // The struct is about 5 KB of UTF-16; marshal through a native buffer.
        var buf = NativeMemory.AllocZeroed((nuint)Marshal.SizeOf<ContextInfo>());
        try
        {
            bool ok = contextInfo(vm, ac, buf) != 0;
            info = ok ? Marshal.PtrToStructure<ContextInfo>((IntPtr)buf) : default;
            return ok;
        }
        finally { NativeMemory.Free(buf); }
    }

    // Children to walk: all of them for ordinary containers; for lists, tables and very wide nodes only the visible ones.
    public static List<long> Children(int vm, long ac, int count, bool visibleOnly)
    {
        var kids = new List<long>();
        if (count <= 0) return kids;
        if (!visibleOnly && count <= MaxVisibleChildren)
        {
            for (int i = 0; i < count; i++) if (childAt(vm, ac, i) is var c and not 0) kids.Add(c);
            return kids;
        }
        VisibleChildren v;
        if (visibleChildren(vm, ac, 0, &v) != 0)
            for (int i = 0; i < Math.Min(v.Count, MaxVisibleChildren); i++) kids.Add(v.Children[i]);
        return kids;
    }

    public static int TextLength(int vm, long ac)
    {
        TextInfo t;
        return textInfo(vm, ac, &t, 0, 0) != 0 ? t.CharCount : 0;
    }

    public static string Text(int vm, long ac, int max)
    {
        int n = Math.Min(TextLength(vm, ac), max);
        if (n <= 0) return "";
        var buf = stackalloc char[n + 1];
        return textRange(vm, ac, 0, n - 1, buf, (short)(n + 1)) != 0 ? new string(buf) : "";
    }

    public static void Release(int vm, long obj) { if (obj != 0) release(vm, obj); }
}
