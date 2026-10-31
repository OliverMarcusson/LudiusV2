//! Win32 helpers: windows, processes, tokens, session and desktop state.
use crate::lists::R;
use windows::Win32::Foundation::{CloseHandle, HANDLE, HWND, LPARAM, RECT};
use windows::Win32::Graphics::Dwm::{DWMWA_CLOAKED, DwmGetWindowAttribute};
use windows::Win32::Security::{GetTokenInformation, TOKEN_QUERY, TokenElevation, TokenUIAccess};
use windows::Win32::System::RemoteDesktop::{ProcessIdToSessionId, WTS_CURRENT_SERVER_HANDLE, WTSFreeMemory, WTSQuerySessionInformationW, WTSSessionInfoEx};
use windows::Win32::System::StationsAndDesktops::{CloseDesktop, DESKTOP_READOBJECTS, DESKTOP_CONTROL_FLAGS, GetUserObjectInformationW, OpenInputDesktop, UOI_NAME};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentProcessId, OpenProcess, OpenProcessToken, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    QueryFullProcessImageNameW,
};
use windows::Win32::UI::WindowsAndMessaging::{EnumWindows, GW_OWNER, GetClassNameW, GetWindow, GetWindowRect, GetWindowTextW, GetWindowThreadProcessId};
use windows::core::{BOOL, PWSTR};

pub fn class_name(h: HWND) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetClassNameW(h, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn title(h: HWND) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(h, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn cloaked(h: HWND) -> bool {
    let mut v = 0u32;
    unsafe { DwmGetWindowAttribute(h, DWMWA_CLOAKED, (&raw mut v).cast(), 4) }.is_ok() && v != 0
}

pub fn owner(h: HWND) -> HWND {
    unsafe { GetWindow(h, GW_OWNER) }.unwrap_or_default()
}

pub fn pid(h: HWND) -> u32 {
    let mut pid = 0;
    unsafe { GetWindowThreadProcessId(h, Some(&mut pid)) };
    pid
}

pub fn rect(h: HWND) -> R {
    let mut r = RECT::default();
    let _ = unsafe { GetWindowRect(h, &mut r) };
    R::new(r.left, r.top, r.right - r.left, r.bottom - r.top)
}

/// Top-level windows in z-order, top first; `f` returns false to stop.
pub fn enum_windows(mut f: impl FnMut(HWND) -> bool) {
    unsafe extern "system" fn cb(h: HWND, l: LPARAM) -> BOOL {
        let f = unsafe { &mut *(l.0 as *mut &mut dyn FnMut(HWND) -> bool) };
        f(h).into()
    }
    let mut f: &mut dyn FnMut(HWND) -> bool = &mut f;
    let _ = unsafe { EnumWindows(Some(cb), LPARAM((&raw mut f) as isize)) };
}

struct Handle(HANDLE);
impl Drop for Handle {
    fn drop(&mut self) {
        let _ = unsafe { CloseHandle(self.0) };
    }
}

fn open(pid: u32) -> Option<Handle> {
    unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }.ok().map(Handle)
}

pub fn process_name(pid: u32) -> String {
    let Some(p) = open(pid) else { return String::new() };
    let mut buf = [0u16; 1024];
    let mut size = buf.len() as u32;
    if unsafe { QueryFullProcessImageNameW(p.0, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut size) }.is_err() {
        return String::new();
    }
    let path = String::from_utf16_lossy(&buf[..size as usize]);
    path.rsplit('\\').next().unwrap_or("").to_string()
}

fn token_flag(process: HANDLE, class: windows::Win32::Security::TOKEN_INFORMATION_CLASS) -> Option<bool> {
    let mut token = HANDLE::default();
    unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) }.ok()?;
    let token = Handle(token);
    let (mut v, mut n) = (0u32, 0u32);
    unsafe { GetTokenInformation(token.0, class, Some((&raw mut v).cast()), 4, &mut n) }.ok()?;
    Some(v != 0)
}

/// Unknown counts as elevated: a process we cannot query is one we cannot read.
pub fn elevated(pid: u32) -> bool {
    open(pid).and_then(|p| token_flag(p.0, TokenElevation)).unwrap_or(true)
}

pub fn self_ui_access() -> bool {
    token_flag(unsafe { GetCurrentProcess() }, TokenUIAccess).unwrap_or(false)
}

pub fn own_session() -> u32 {
    let mut s = 0;
    let _ = unsafe { ProcessIdToSessionId(GetCurrentProcessId(), &mut s) };
    s
}

pub fn session_locked() -> bool {
    let mut session = 0;
    if unsafe { ProcessIdToSessionId(GetCurrentProcessId(), &mut session) }.is_err() {
        return false;
    }
    let (mut buf, mut bytes) = (PWSTR::null(), 0u32);
    if unsafe { WTSQuerySessionInformationW(Some(WTS_CURRENT_SERVER_HANDLE), session, WTSSessionInfoEx, &mut buf, &mut bytes) }.is_err() {
        return false;
    }
    // WTSINFOEXW: Level, then WTSINFOEX_LEVEL1_W at offset 8; SessionFlags (offset 16) is 0 when locked.
    let locked = bytes >= 20 && unsafe { (buf.0 as *const u8).add(16).cast::<i32>().read_unaligned() } == 0;
    unsafe { WTSFreeMemory(buf.0.cast()) };
    locked
}

pub fn input_desktop_is_default() -> bool {
    let Ok(d) = (unsafe { OpenInputDesktop(DESKTOP_CONTROL_FLAGS(0), false, DESKTOP_READOBJECTS) }) else { return false };
    let mut buf = [0u16; 64];
    let ok = unsafe { GetUserObjectInformationW(HANDLE(d.0), UOI_NAME, Some(buf.as_mut_ptr().cast()), 128, None) }.is_ok();
    let _ = unsafe { CloseDesktop(d) };
    let name = String::from_utf16_lossy(&buf);
    ok && name.trim_end_matches('\0').eq_ignore_ascii_case("Default")
}
