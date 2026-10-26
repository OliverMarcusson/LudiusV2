using System.Runtime.InteropServices;
using Interop.UIAutomationClient;
using Vortice.Direct3D11;
using Vortice.DXGI;
using static Interop.UIAutomationClient.UIA_EventIds;
using static Interop.UIAutomationClient.UIA_PropertyIds;

namespace Ludius.Observer;

// The screen has settled when neither UIA (structure/property events from the
// foreground window, focus, windows and menus anywhere) nor DXGI desktop
// duplication (dirty rectangles) reported a change for the quiet interval.
// Events only time the snapshot; the table is always rebuilt.
sealed class Settle : IUIAutomationEventHandler, IUIAutomationStructureChangedEventHandler,
    IUIAutomationPropertyChangedEventHandler, IUIAutomationFocusChangedEventHandler
{
    readonly IUIAutomation2 uia;
    long lastChange = Environment.TickCount64;
    IUIAutomationElement? tracked;
    IntPtr trackedHwnd;
    volatile bool pixels;

    public bool PixelSettle => pixels;

    public Settle(IUIAutomation2 uia)
    {
        this.uia = uia;
        var root = uia.GetRootElement();
        uia.AddFocusChangedEventHandler(null, this);
        foreach (var id in new[] { UIA_Window_WindowOpenedEventId, UIA_MenuOpenedEventId, UIA_MenuClosedEventId })
            uia.AddAutomationEventHandler(id, root, TreeScope.TreeScope_Subtree, null, this);
        new Thread(PixelLoop) { IsBackground = true, Name = "dxgi" }.Start();
    }

    void Hit() => Interlocked.Exchange(ref lastChange, Environment.TickCount64);

    public void HandleAutomationEvent(IUIAutomationElement sender, int eventId) => Hit();
    public void HandleStructureChangedEvent(IUIAutomationElement sender, StructureChangeType changeType, int[] runtimeId) => Hit();
    public void HandlePropertyChangedEvent(IUIAutomationElement sender, int propertyId, object newValue) => Hit();
    public void HandleFocusChangedEvent(IUIAutomationElement sender) => Hit();

    // Follow the foreground window with subtree-scoped handlers. Called from
    // the request thread, never from inside a handler.
    void Track()
    {
        var fg = Native.GetForegroundWindow();
        if (fg == trackedHwnd) return;
        try
        {
            if (tracked != null)
            {
                uia.RemoveStructureChangedEventHandler(tracked, this);
                uia.RemovePropertyChangedEventHandler(tracked, this);
            }
        }
        catch (Exception e) when (e is COMException or UnauthorizedAccessException) { }
        tracked = null;
        trackedHwnd = fg;
        if (fg == 0) return;
        try
        {
            tracked = uia.ElementFromHandle(fg);
            uia.AddStructureChangedEventHandler(tracked, TreeScope.TreeScope_Subtree, null, this);
            uia.AddPropertyChangedEventHandler(tracked, TreeScope.TreeScope_Subtree, null, this,
                [UIA_NamePropertyId, UIA_ValueValuePropertyId, UIA_IsEnabledPropertyId, UIA_ToggleToggleStatePropertyId,
                 UIA_ExpandCollapseExpandCollapseStatePropertyId, UIA_SelectionItemIsSelectedPropertyId]);
        }
        catch (Exception e) when (e is COMException or UnauthorizedAccessException) { tracked = null; }
        Hit();
    }

    public (bool settled, uint ms) Wait(uint quietMs, uint timeoutMs)
    {
        var start = Environment.TickCount64;
        Interlocked.Exchange(ref lastChange, start);
        while (true)
        {
            Track();
            var now = Environment.TickCount64;
            if (now - Interlocked.Read(ref lastChange) >= quietMs) return (true, (uint)(now - start));
            if (now - start >= timeoutMs) return (false, (uint)(now - start));
            Thread.Sleep(25);
        }
    }

    void PixelLoop()
    {
        while (true)
        {
            try
            {
                using var dup = Duplicate();
                pixels = true;
                while (true)
                {
                    var r = dup.AcquireNextFrame(100, out var info, out var res);
                    if (r == Vortice.DXGI.ResultCode.WaitTimeout) continue;
                    r.CheckError();
                    res?.Dispose();
                    // Pointer-only updates have no present time; the caret and real changes do.
                    if (info.LastPresentTime != 0 && info.TotalMetadataBufferSize > 0) Hit();
                    dup.ReleaseFrame();
                }
            }
            catch (Exception)
            {
                // Lost on desktop switches (lock, UAC); retry quietly.
                pixels = false;
                Thread.Sleep(1000);
            }
        }
    }

    static IDXGIOutputDuplication Duplicate()
    {
        using var factory = DXGI.CreateDXGIFactory1<IDXGIFactory1>();
        for (uint a = 0; factory.EnumAdapters1(a, out var adapter).Success; a++)
        {
            using (adapter)
            {
                if (adapter.EnumOutputs(0, out var output).Failure) continue;
                using (output)
                using (var output1 = output.QueryInterface<IDXGIOutput1>())
                {
                    D3D11.D3D11CreateDevice(adapter, Vortice.Direct3D.DriverType.Unknown, DeviceCreationFlags.None, null, out ID3D11Device? device).CheckError();
                    using (device)
                        return output1.DuplicateOutput(device);
                }
            }
        }
        throw new InvalidOperationException("no output to duplicate");
    }
}
