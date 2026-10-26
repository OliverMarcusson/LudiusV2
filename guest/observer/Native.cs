using System.Runtime.InteropServices;
using System.Text;

namespace Ludius.Observer;

static partial class Native
{
    public const int GW_OWNER = 4;
    public const int GWL_STYLE = -16;
    public const int GWL_EXSTYLE = -20;
    public const long WS_POPUP = 0x80000000;
    public const long WS_EX_TOOLWINDOW = 0x80;
    public const int DWMWA_CLOAKED = 14;
    public const int IDC_WAIT = 32514;
    public const int IDC_APPSTARTING = 32650;

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; }

    [StructLayout(LayoutKind.Sequential)]
    public struct CURSORINFO { public int cbSize; public int flags; public IntPtr hCursor; public POINT ptScreenPos; }

    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

    [LibraryImport("user32.dll")] public static partial IntPtr GetForegroundWindow();
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool EnumWindows(EnumWindowsProc proc, IntPtr lParam);
    [LibraryImport("user32.dll")] public static partial IntPtr GetWindow(IntPtr hwnd, int cmd);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool IsWindowVisible(IntPtr hwnd);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool IsWindowEnabled(IntPtr hwnd);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool IsHungAppWindow(IntPtr hwnd);
    [LibraryImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] public static partial long GetWindowLongPtr(IntPtr hwnd, int index);
    [LibraryImport("user32.dll")] public static partial uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool GetCursorInfo(ref CURSORINFO info);
    [LibraryImport("user32.dll", EntryPoint = "LoadCursorW")] public static partial IntPtr LoadCursor(IntPtr instance, IntPtr name);
    [LibraryImport("user32.dll")] public static partial int GetSystemMetrics(int index);
    [LibraryImport("user32.dll")] public static partial uint GetDpiForSystem();
    [LibraryImport("user32.dll", EntryPoint = "OpenInputDesktop")] public static partial IntPtr OpenInputDesktop(uint flags, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint access);
    [LibraryImport("user32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", EntryPoint = "GetUserObjectInformationW", CharSet = CharSet.Unicode)]
    public static extern bool GetUserObjectInformation(IntPtr obj, int index, [Out] char[] info, int length, out int needed);
    [LibraryImport("dwmapi.dll")] public static partial int DwmGetWindowAttribute(IntPtr hwnd, int attr, out int value, int size);
    [LibraryImport("kernel32.dll")] public static partial IntPtr OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint pid);
    [LibraryImport("kernel32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", EntryPoint = "QueryFullProcessImageNameW", CharSet = CharSet.Unicode)]
    public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, [Out] char[] name, ref int size);
    [LibraryImport("advapi32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [LibraryImport("advapi32.dll")][return: MarshalAs(UnmanagedType.Bool)]
    public static partial bool GetTokenInformation(IntPtr token, int cls, out int info, int length, out int returned);
    [LibraryImport("kernel32.dll")] public static partial IntPtr GetCurrentProcess();
    [LibraryImport("kernel32.dll")][return: MarshalAs(UnmanagedType.Bool)] public static partial bool ProcessIdToSessionId(uint pid, out uint session);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, StringBuilder name, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);

    public static string ClassName(IntPtr hwnd) { var sb = new StringBuilder(256); GetClassName(hwnd, sb, sb.Capacity); return sb.ToString(); }
    public static string Title(IntPtr hwnd) { var sb = new StringBuilder(512); GetWindowText(hwnd, sb, sb.Capacity); return sb.ToString(); }
    public static bool Cloaked(IntPtr hwnd) => DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, out var v, 4) == 0 && v != 0;
    public static IntPtr Owner(IntPtr hwnd) => GetWindow(hwnd, GW_OWNER);

    public static string ProcessName(uint pid)
    {
        var h = OpenProcess(0x1000 /* QUERY_LIMITED_INFORMATION */, false, pid);
        if (h == IntPtr.Zero) return "";
        try
        {
            var buf = new char[1024]; int size = buf.Length;
            return QueryFullProcessImageName(h, 0, buf, ref size) ? Path.GetFileName(new string(buf, 0, size)) : "";
        }
        finally { CloseHandle(h); }
    }

    // True when the process is elevated, or its token is out of reach (which,
    // from a medium-integrity observer, means the same thing).
    public static bool Elevated(uint pid)
    {
        var h = OpenProcess(0x1000, false, pid);
        if (h == IntPtr.Zero) return true;
        try
        {
            if (!OpenProcessToken(h, 0x0008 /* TOKEN_QUERY */, out var token)) return true;
            try { return GetTokenInformation(token, 20 /* TokenElevation */, out var elevated, 4, out _) && elevated != 0; }
            finally { CloseHandle(token); }
        }
        finally { CloseHandle(h); }
    }

    public static bool SelfUiAccess()
    {
        if (!OpenProcessToken(GetCurrentProcess(), 0x0008, out var token)) return false;
        try { return GetTokenInformation(token, 26 /* TokenUIAccess */, out var ui, 4, out _) && ui != 0; }
        finally { CloseHandle(token); }
    }

    // The input desktop is "Default" while the user is at the desktop; Winlogon
    // (lock screen, UAC secure desktop) cannot even be opened from the session.
    public static bool InputDesktopIsDefault()
    {
        var d = OpenInputDesktop(0, false, 0x0001 /* DESKTOP_READOBJECTS */);
        if (d == IntPtr.Zero) return false;
        try
        {
            var buf = new char[64];
            return GetUserObjectInformation(d, 2 /* UOI_NAME */, buf, buf.Length * 2, out _) &&
                   new string(buf).TrimEnd('\0').Equals("Default", StringComparison.OrdinalIgnoreCase);
        }
        finally { CloseDesktop(d); }
    }
}
