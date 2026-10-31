//! Java Access Bridge (WindowsAccessBridge-64.dll), loaded at start when a JRE is installed.
use std::sync::OnceLock;
use std::sync::mpsc;
use windows::Win32::Foundation::{HMODULE, HWND};
use windows::Win32::System::LibraryLoader::{GetProcAddress, LOAD_LIBRARY_SEARCH_SYSTEM32, LoadLibraryExW, LoadLibraryW};
use windows::Win32::System::Registry::{HKEY, HKEY_LOCAL_MACHINE, KEY_READ, RRF_RT_REG_SZ, RegCloseKey, RegGetValueW, RegOpenKeyExW};
use windows::Win32::UI::WindowsAndMessaging::{
    ChangeWindowMessageFilter, DispatchMessageW, GetMessageW, MSG, MSGFLT_ADD, RegisterWindowMessageW, TranslateMessage, WM_COPYDATA, WM_USER,
};
use windows::core::{BOOL, HSTRING, PCSTR};

pub const MAX_VISIBLE_CHILDREN: usize = 256;

#[repr(C)]
pub struct ContextInfo {
    name: [u16; 1024],
    description: [u16; 1024],
    role: [u16; 256],
    role_en_us: [u16; 256],
    states: [u16; 256],
    states_en_us: [u16; 256],
    pub index_in_parent: i32,
    pub children_count: i32,
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
    pub accessible_component: i32,
    pub accessible_action: i32,
    pub accessible_selection: i32,
    pub accessible_text: i32,
    pub accessible_interfaces: i32,
}

fn wide(s: &[u16]) -> String {
    String::from_utf16_lossy(&s[..s.iter().position(|&c| c == 0).unwrap_or(s.len())])
}

impl ContextInfo {
    pub fn name(&self) -> String {
        wide(&self.name)
    }
    pub fn description(&self) -> String {
        wide(&self.description)
    }
    pub fn role_en_us(&self) -> String {
        wide(&self.role_en_us)
    }
    pub fn states_en_us(&self) -> String {
        wide(&self.states_en_us)
    }
}

#[repr(C)]
struct TextInfo {
    char_count: i32,
    caret_index: i32,
    index_at_point: i32,
}

#[repr(C)]
struct VisibleChildren {
    count: i32,
    children: [i64; MAX_VISIBLE_CHILDREN],
}

struct Api {
    is_java_window: unsafe extern "C" fn(HWND) -> BOOL,
    context_from_hwnd: unsafe extern "C" fn(HWND, *mut i32, *mut i64) -> BOOL,
    context_info: unsafe extern "C" fn(i32, i64, *mut ContextInfo) -> BOOL,
    child_at: unsafe extern "C" fn(i32, i64, i32) -> i64,
    visible_children: unsafe extern "C" fn(i32, i64, i32, *mut VisibleChildren) -> BOOL,
    text_info: unsafe extern "C" fn(i32, i64, *mut TextInfo, i32, i32) -> BOOL,
    text_range: unsafe extern "C" fn(i32, i64, i32, i32, *mut u16, i16) -> BOOL,
    release: unsafe extern "C" fn(i32, i64),
}

static API: OnceLock<Api> = OnceLock::new();

fn reg_string(key: HKEY, sub: &str, value: &str) -> Option<String> {
    let mut buf = [0u16; 1024];
    let mut size = (buf.len() * 2) as u32;
    unsafe { RegGetValueW(key, &HSTRING::from(sub), &HSTRING::from(value), RRF_RT_REG_SZ, None, Some(buf.as_mut_ptr().cast()), Some(&mut size)) }
        .ok()
        .ok()?;
    Some(wide(&buf))
}

fn load() -> Option<HMODULE> {
    const NAME: &str = "WindowsAccessBridge-64.dll";
    let mut homes: Vec<String> = std::env::var("JAVA_HOME").into_iter().collect();
    for kind in ["JRE", "JDK", "Java Runtime Environment"] {
        let base = format!(r"SOFTWARE\JavaSoft\{kind}");
        let mut k = HKEY::default();
        if unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, &HSTRING::from(&base), None, KEY_READ, &mut k) }.is_err() {
            continue;
        }
        if let Some(v) = reg_string(k, "", "CurrentVersion")
            && let Some(home) = reg_string(k, &v, "JavaHome")
        {
            homes.push(home);
        }
        let _ = unsafe { RegCloseKey(k) };
    }
    for home in homes.iter().filter(|h| !h.is_empty()) {
        let path = std::path::Path::new(home).join("bin").join(NAME);
        if let Ok(h) = unsafe { LoadLibraryW(&HSTRING::from(path.as_os_str())) } {
            return Some(h);
        }
    }
    unsafe { LoadLibraryExW(&HSTRING::from(NAME), None, LOAD_LIBRARY_SEARCH_SYSTEM32) }.ok()
}

/// Loads the bridge and starts its message pump; without a JRE, Java windows read as plain UIA.
#[allow(clippy::missing_transmute_annotations)]
pub fn start() {
    let Some(dll) = load() else { return };
    macro_rules! sym {
        ($name:literal) => {{
            let p = unsafe { GetProcAddress(dll, PCSTR(concat!($name, "\0").as_ptr())) };
            match p {
                Some(p) => unsafe { std::mem::transmute::<unsafe extern "system" fn() -> isize, _>(p) },
                None => return,
            }
        }};
    }
    let windows_run: unsafe extern "C" fn() = sym!("Windows_run");
    let api = Api {
        is_java_window: sym!("isJavaWindow"),
        context_from_hwnd: sym!("getAccessibleContextFromHWND"),
        context_info: sym!("getAccessibleContextInfo"),
        child_at: sym!("getAccessibleChildFromContext"),
        visible_children: sym!("getVisibleChildren"),
        text_info: sym!("getAccessibleTextInfo"),
        text_range: sym!("getAccessibleTextRange"),
        release: sym!("releaseJavaObject"),
    };
    // A Limited observer must accept the bridge's messages from the (other-integrity) JVM.
    for m in [WM_COPYDATA, WM_USER + 0x1000, WM_USER + 0x1001, WM_USER + 0x1002, WM_USER + 0x1003] {
        let _ = unsafe { ChangeWindowMessageFilter(m, MSGFLT_ADD) };
    }
    for name in ["AccessBridge-FromJava-Hello", "AccessBridge-FromWindows-Hello"] {
        let m = unsafe { RegisterWindowMessageW(&HSTRING::from(name)) };
        if m != 0 {
            let _ = unsafe { ChangeWindowMessageFilter(m, MSGFLT_ADD) };
        }
    }
    let (tx, rx) = mpsc::channel();
    let pump = move || {
        unsafe { windows_run() };
        let _ = tx.send(());
        let mut m = MSG::default();
        while unsafe { GetMessageW(&mut m, None, 0, 0) }.0 > 0 {
            unsafe {
                let _ = TranslateMessage(&m);
                DispatchMessageW(&m);
            }
        }
    };
    if std::thread::Builder::new().name("jab".into()).spawn(pump).is_err() || rx.recv().is_err() {
        return;
    }
    std::thread::sleep(std::time::Duration::from_secs(1));
    let _ = API.set(api);
}

pub fn is_java(h: HWND) -> bool {
    API.get().is_some_and(|a| unsafe { (a.is_java_window)(h) }.as_bool())
}

pub fn root(h: HWND) -> Option<(i32, i64)> {
    let a = API.get()?;
    let (mut vm, mut ac) = (0, 0);
    unsafe { (a.context_from_hwnd)(h, &mut vm, &mut ac) }.as_bool().then_some((vm, ac))
}

pub fn info(vm: i32, ac: i64) -> Option<Box<ContextInfo>> {
    let a = API.get()?;
    // SAFETY: ContextInfo is plain integers and u16 arrays; all-zero is a valid value.
    let mut info: Box<ContextInfo> = unsafe { Box::new_zeroed().assume_init() };
    unsafe { (a.context_info)(vm, ac, &mut *info) }.as_bool().then_some(info)
}

pub fn children(vm: i32, ac: i64, count: i32, visible_only: bool) -> Vec<i64> {
    let Some(a) = API.get() else { return vec![] };
    if count <= 0 {
        return vec![];
    }
    if !visible_only && count as usize <= MAX_VISIBLE_CHILDREN {
        return (0..count).map(|i| unsafe { (a.child_at)(vm, ac, i) }).filter(|&c| c != 0).collect();
    }
    let mut v: Box<VisibleChildren> = unsafe { Box::new_zeroed().assume_init() };
    if !unsafe { (a.visible_children)(vm, ac, 0, &mut *v) }.as_bool() {
        return vec![];
    }
    v.children[..(v.count.max(0) as usize).min(MAX_VISIBLE_CHILDREN)].to_vec()
}

pub fn text_length(vm: i32, ac: i64) -> i32 {
    let Some(a) = API.get() else { return 0 };
    let mut t = TextInfo { char_count: 0, caret_index: 0, index_at_point: 0 };
    if unsafe { (a.text_info)(vm, ac, &mut t, 0, 0) }.as_bool() { t.char_count } else { 0 }
}

pub fn text(vm: i32, ac: i64, max: i32) -> String {
    let Some(a) = API.get() else { return String::new() };
    let n = text_length(vm, ac).min(max);
    if n <= 0 {
        return String::new();
    }
    let mut buf = vec![0u16; n as usize + 1];
    if unsafe { (a.text_range)(vm, ac, 0, n - 1, buf.as_mut_ptr(), (n + 1) as i16) }.as_bool() { wide(&buf) } else { String::new() }
}

pub fn release(vm: i32, obj: i64) {
    if obj != 0
        && let Some(a) = API.get()
    {
        unsafe { (a.release)(vm, obj) };
    }
}
