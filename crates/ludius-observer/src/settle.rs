//! Settle: the UI is quiet when no UIA event and no new desktop frame arrived for `quiet_ms`.
use crate::{native, watchdog};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_UNKNOWN;
use windows::Win32::Graphics::Direct3D11::{D3D11_CREATE_DEVICE_FLAG, D3D11_SDK_VERSION, D3D11CreateDevice, ID3D11Device};
use windows::Win32::Graphics::Dxgi::{CreateDXGIFactory1, DXGI_ERROR_WAIT_TIMEOUT, DXGI_OUTDUPL_FRAME_INFO, IDXGIFactory1, IDXGIOutput1, IDXGIOutputDuplication};
use windows::Win32::System::SystemInformation::GetTickCount64;
use windows::Win32::System::Variant::VARIANT;
use windows::Win32::UI::Accessibility::*;
use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;
use windows::core::{IUnknown, Interface, Ref, Result, implement};

pub fn now() -> u64 {
    unsafe { GetTickCount64() }
}

#[implement(IUIAutomationEventHandler, IUIAutomationStructureChangedEventHandler, IUIAutomationPropertyChangedEventHandler, IUIAutomationFocusChangedEventHandler)]
struct Hits(Arc<AtomicU64>);

impl Hits {
    fn hit(&self) -> Result<()> {
        self.0.store(now(), Ordering::Relaxed);
        Ok(())
    }
}

impl IUIAutomationEventHandler_Impl for Hits_Impl {
    fn HandleAutomationEvent(&self, _: Ref<IUIAutomationElement>, _: UIA_EVENT_ID) -> Result<()> {
        self.hit()
    }
}
impl IUIAutomationStructureChangedEventHandler_Impl for Hits_Impl {
    fn HandleStructureChangedEvent(&self, _: Ref<IUIAutomationElement>, _: StructureChangeType, _: *const windows::Win32::System::Com::SAFEARRAY) -> Result<()> {
        self.hit()
    }
}
impl IUIAutomationPropertyChangedEventHandler_Impl for Hits_Impl {
    fn HandlePropertyChangedEvent(&self, _: Ref<IUIAutomationElement>, _: UIA_PROPERTY_ID, _: &VARIANT) -> Result<()> {
        self.hit()
    }
}
impl IUIAutomationFocusChangedEventHandler_Impl for Hits_Impl {
    fn HandleFocusChangedEvent(&self, _: Ref<IUIAutomationElement>) -> Result<()> {
        self.hit()
    }
}

pub struct Settle {
    uia: IUIAutomation2,
    last: Arc<AtomicU64>,
    pixels: Arc<AtomicBool>,
    events: IUIAutomationEventHandler,
    structure: IUIAutomationStructureChangedEventHandler,
    property: IUIAutomationPropertyChangedEventHandler,
    focus: IUIAutomationFocusChangedEventHandler,
    globals: bool,
    tracked: Option<IUIAutomationElement>,
    tracked_hwnd: HWND,
}

impl Settle {
    pub fn new(uia: IUIAutomation2) -> Result<Settle> {
        let last = Arc::new(AtomicU64::new(now()));
        let unk: IUnknown = Hits(last.clone()).into();
        let mut s = Settle {
            uia,
            last,
            pixels: Arc::new(AtomicBool::new(false)),
            events: unk.cast()?,
            structure: unk.cast()?,
            property: unk.cast()?,
            focus: unk.cast()?,
            globals: false,
            tracked: None,
            tracked_hwnd: HWND::default(),
        };
        s.set_globals(true);
        let (pixels, last) = (s.pixels.clone(), s.last.clone());
        std::thread::Builder::new().name("dxgi".into()).spawn(move || pixel_loop(&pixels, &last)).map_err(|_| windows::core::Error::empty())?;
        Ok(s)
    }

    pub fn pixel_settle(&self) -> bool {
        self.pixels.load(Ordering::Relaxed)
    }

    fn hit(&self) {
        self.last.store(now(), Ordering::Relaxed);
    }

    fn set_globals(&mut self, on: bool) {
        if on == self.globals {
            return;
        }
        watchdog::step(if on { "settle: add handlers" } else { "settle: remove handlers" });
        let r = unsafe {
            if on {
                let root = self.uia.GetRootElement();
                self.uia.AddFocusChangedEventHandler(None, &self.focus).and(root).and_then(|root| {
                    for id in [UIA_Window_WindowOpenedEventId, UIA_MenuOpenedEventId, UIA_MenuClosedEventId] {
                        self.uia.AddAutomationEventHandler(id, &root, TreeScope_Subtree, None, &self.events)?;
                    }
                    Ok(())
                })
            } else {
                self.uia.RemoveAllEventHandlers()
            }
        };
        if r.is_ok() {
            self.globals = on;
        }
    }

    /// Follows the foreground window: structure and property events from its subtree count as changes.
    fn track(&mut self) {
        let fg = unsafe { GetForegroundWindow() };
        if fg == self.tracked_hwnd {
            return;
        }
        if let Some(t) = self.tracked.take() {
            watchdog::step("settle: untrack");
            unsafe {
                let _ = self.uia.RemoveStructureChangedEventHandler(&t, &self.structure);
                let _ = self.uia.RemovePropertyChangedEventHandler(&t, &self.property);
            }
        }
        self.tracked_hwnd = fg;
        if fg.is_invalid() {
            return;
        }
        // Excel floods UIA events; settle on pixels alone there.
        let excel = native::process_name(native::pid(fg)).eq_ignore_ascii_case("EXCEL.EXE");
        self.set_globals(!excel);
        if excel {
            self.hit();
            return;
        }
        watchdog::step(&format!("settle: track {}", native::class_name(fg)));
        let props = [
            UIA_NamePropertyId,
            UIA_ValueValuePropertyId,
            UIA_IsEnabledPropertyId,
            UIA_ToggleToggleStatePropertyId,
            UIA_ExpandCollapseExpandCollapseStatePropertyId,
            UIA_SelectionItemIsSelectedPropertyId,
        ];
        let tracked = unsafe {
            self.uia.ElementFromHandle(fg).and_then(|t| {
                self.uia.AddStructureChangedEventHandler(&t, TreeScope_Subtree, None, &self.structure)?;
                self.uia.AddPropertyChangedEventHandlerNativeArray(&t, TreeScope_Subtree, None, &self.property, &props)?;
                Ok(t)
            })
        };
        self.tracked = tracked.ok();
        self.hit();
    }

    pub fn wait(&mut self, quiet_ms: u32, timeout_ms: u32) -> (bool, u32) {
        let start = now();
        self.last.store(start, Ordering::Relaxed);
        loop {
            self.track();
            let now = now();
            if now.saturating_sub(self.last.load(Ordering::Relaxed)) >= quiet_ms as u64 {
                return (true, (now - start) as u32);
            }
            if now - start >= timeout_ms as u64 {
                return (false, (now - start) as u32);
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
}

fn duplicate() -> Result<IDXGIOutputDuplication> {
    let factory: IDXGIFactory1 = unsafe { CreateDXGIFactory1()? };
    let mut a = 0;
    while let Ok(adapter) = unsafe { factory.EnumAdapters1(a) } {
        a += 1;
        let Ok(output) = (unsafe { adapter.EnumOutputs(0) }) else { continue };
        let output1: IDXGIOutput1 = output.cast()?;
        let mut device: Option<ID3D11Device> = None;
        unsafe {
            D3D11CreateDevice(&adapter, D3D_DRIVER_TYPE_UNKNOWN, Default::default(), D3D11_CREATE_DEVICE_FLAG(0), None, D3D11_SDK_VERSION, Some(&mut device), None, None)?;
        }
        let device = device.ok_or_else(windows::core::Error::empty)?;
        return unsafe { output1.DuplicateOutput(&device) };
    }
    Err(windows::core::Error::empty())
}

/// A presented desktop frame with changed regions counts as a change.
fn pixel_loop(pixels: &AtomicBool, last: &AtomicU64) {
    loop {
        if let Ok(dup) = duplicate() {
            pixels.store(true, Ordering::Relaxed);
            loop {
                let mut info = DXGI_OUTDUPL_FRAME_INFO::default();
                let mut res = None;
                match unsafe { dup.AcquireNextFrame(100, &mut info, &mut res) } {
                    Ok(()) => {
                        if info.LastPresentTime != 0 && info.TotalMetadataBufferSize > 0 {
                            last.store(now(), Ordering::Relaxed);
                        }
                        drop(res);
                        if unsafe { dup.ReleaseFrame() }.is_err() {
                            break;
                        }
                    }
                    Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => {}
                    Err(_) => break,
                }
            }
        }
        pixels.store(false, Ordering::Relaxed);
        std::thread::sleep(Duration::from_secs(1));
    }
}
