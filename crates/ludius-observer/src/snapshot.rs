//! Snapshot: the windows on screen and their actionable elements, read through UIA (or the Java bridge).
use crate::lists::*;
use crate::{java, native, watchdog};
use ludius_proto::v1::{Element, Snapshot, Window, window::Kind};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicIsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use windows::Win32::Foundation::{E_ACCESSDENIED, E_INVALIDARG, E_POINTER, HWND, POINT};
use windows::Win32::System::Variant::*;
use windows::Win32::UI::Accessibility::*;
use windows::Win32::UI::HiDpi::GetDpiForSystem;
use windows::Win32::UI::Input::KeyboardAndMouse::IsWindowEnabled;
use windows::Win32::UI::WindowsAndMessaging::*;
use windows::core::{BSTR, HRESULT};

const CAP: usize = 200;
const MAX_VALUE: usize = 300;
const UIA_E_TIMEOUT: HRESULT = HRESULT(0x80131505_u32 as i32);
const UIA_E_ELEMENTNOTAVAILABLE: HRESULT = HRESULT(0x80040201_u32 as i32);

const PROPS: &[UIA_PROPERTY_ID] = &[
    UIA_ControlTypePropertyId, UIA_LocalizedControlTypePropertyId, UIA_NamePropertyId, UIA_AutomationIdPropertyId,
    UIA_ClassNamePropertyId, UIA_FrameworkIdPropertyId, UIA_BoundingRectanglePropertyId, UIA_HelpTextPropertyId,
    UIA_IsEnabledPropertyId, UIA_HasKeyboardFocusPropertyId, UIA_IsKeyboardFocusablePropertyId, UIA_IsPasswordPropertyId,
    UIA_LabeledByPropertyId, UIA_ValueValuePropertyId, UIA_ValueIsReadOnlyPropertyId, UIA_ToggleToggleStatePropertyId,
    UIA_SelectionItemIsSelectedPropertyId, UIA_RangeValueValuePropertyId, UIA_ExpandCollapseExpandCollapseStatePropertyId,
    UIA_ScrollVerticallyScrollablePropertyId, UIA_ScrollVerticalScrollPercentPropertyId, UIA_GridRowCountPropertyId,
    UIA_IsInvokePatternAvailablePropertyId, UIA_IsValuePatternAvailablePropertyId, UIA_IsTogglePatternAvailablePropertyId,
    UIA_IsSelectionItemPatternAvailablePropertyId, UIA_IsExpandCollapsePatternAvailablePropertyId,
    UIA_IsScrollPatternAvailablePropertyId, UIA_IsRangeValuePatternAvailablePropertyId, UIA_IsTextPatternAvailablePropertyId,
    UIA_LegacyIAccessibleNamePropertyId, UIA_LegacyIAccessibleStatePropertyId, UIA_LegacyIAccessibleDefaultActionPropertyId,
    UIA_LegacyIAccessibleValuePropertyId,
];

/// `--once --title`: observe this window as the foreground.
pub static FOREGROUND: AtomicIsize = AtomicIsize::new(0);

struct Raw {
    e: Element,
    kind: Kind,
    actionable: bool,
    context: String,
    busy: bool,
    content: Option<R>,
}

type El = IUIAutomationElement;

// --- cached property reads ----------------------------------------------------------------------

fn var(c: &El, id: UIA_PROPERTY_ID) -> Option<VARIANT> {
    unsafe { c.GetCachedPropertyValue(id) }.ok()
}

fn vt(v: &VARIANT) -> VARENUM {
    unsafe { v.Anonymous.Anonymous.vt }
}

fn s(c: &El, id: UIA_PROPERTY_ID) -> String {
    match var(c, id) {
        Some(v) if vt(&v) == VT_BSTR => unsafe { v.Anonymous.Anonymous.Anonymous.bstrVal.to_string() },
        _ => String::new(),
    }
}

fn b(c: &El, id: UIA_PROPERTY_ID) -> bool {
    matches!(var(c, id), Some(v) if vt(&v) == VT_BOOL && unsafe { v.Anonymous.Anonymous.Anonymous.boolVal }.as_bool())
}

fn i(c: &El, id: UIA_PROPERTY_ID) -> i32 {
    match var(c, id) {
        Some(v) if vt(&v) == VT_I4 || vt(&v) == VT_INT => unsafe { v.Anonymous.Anonymous.Anonymous.lVal },
        Some(v) if vt(&v) == VT_UI4 || vt(&v) == VT_UINT => unsafe { v.Anonymous.Anonymous.Anonymous.ulVal as i32 },
        _ => 0,
    }
}

fn f(c: &El, id: UIA_PROPERTY_ID) -> Option<f64> {
    match var(c, id) {
        Some(v) if vt(&v) == VT_R8 => Some(unsafe { v.Anonymous.Anonymous.Anonymous.dblVal }),
        _ => None,
    }
}

fn bounds(c: &El) -> R {
    match unsafe { c.CachedBoundingRectangle() } {
        Ok(r) => R::new(r.left, r.top, r.right - r.left, r.bottom - r.top),
        Err(_) => R::default(),
    }
}

fn role(c: &El) -> String {
    role_name(i(c, UIA_ControlTypePropertyId)).map_or_else(|| s(c, UIA_LocalizedControlTypePropertyId), str::to_string)
}

/// Cached children; a missing child array is no children.
fn kids(c: &El) -> windows::core::Result<Vec<El>> {
    match unsafe { c.GetCachedChildren() } {
        Ok(a) => {
            let n = unsafe { a.Length() }?;
            (0..n).map(|k| unsafe { a.GetElement(k) }).collect()
        }
        Err(e) if e.code() == E_POINTER || e.code().is_ok() => Ok(vec![]),
        Err(e) => Err(e),
    }
}

// --- windows --------------------------------------------------------------------------------------

fn overlaps(h: HWND, a: R) -> bool {
    let b = native::rect(h);
    b.x < a.x + a.w && a.x < b.x + b.w && b.y < a.y + a.h && a.y < b.y + b.h
}

fn style(h: HWND, index: WINDOW_LONG_PTR_INDEX) -> i64 {
    unsafe { GetWindowLongPtrW(h, index) as i64 }
}

const SHELL: &[&str] = &["Shell_TrayWnd", "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow", "TopLevelWindowForOverflowXamlIsland", "Progman", "WorkerW"];

/// Foreground window, its owned windows, its popups (menus, overlapping popups above it) and the shell.
fn collect_windows() -> Vec<(HWND, Kind)> {
    let forced = FOREGROUND.load(Ordering::Relaxed);
    let fg = if forced != 0 { HWND(forced as *mut _) } else { unsafe { GetForegroundWindow() } };
    let fg_pid = native::pid(fg);
    let fg_rect = native::rect(fg);
    let fg_class = native::class_name(fg);
    let (mut owned, mut popups, mut shell) = (vec![], vec![], vec![]);
    let mut below = false;
    native::enum_windows(|h| {
        if h == fg {
            below = true;
            return true;
        }
        if !unsafe { IsWindowVisible(h) }.as_bool() || native::cloaked(h) {
            return true;
        }
        let cls = native::class_name(h);
        if cls == "MSO_BORDEREFFECT_WINDOW_CLASS" {
            return true;
        }
        let mut o = native::owner(h);
        while !o.is_invalid() {
            if o == fg {
                owned.push((h, Kind::Owned));
                return true;
            }
            o = native::owner(o);
        }
        let pid = native::pid(h);
        let popup = style(h, GWL_STYLE) & WS_POPUP.0 as i64 != 0;
        if SHELL.contains(&cls.as_str()) {
            let desktop = cls == "Progman" || cls == "WorkerW";
            if !desktop || ["Shell_TrayWnd", "Progman", "WorkerW"].contains(&fg_class.as_str()) || fg.is_invalid() {
                shell.push((h, Kind::Shell));
            }
        } else if cls == "#32768" || (!below && pid == fg_pid && !fg.is_invalid() && popup && overlaps(h, fg_rect)) {
            popups.push((h, Kind::Popup));
        }
        true
    });
    let mut all = vec![];
    if !fg.is_invalid() {
        all.push((fg, Kind::Foreground));
    }
    all.extend(owned);
    all.extend(popups);
    all.extend(shell);
    all
}

/// Top-level application windows (the taskbar's view).
fn app_windows() -> Vec<HWND> {
    const NOT_APPS: &[&str] = &["Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"];
    let mut list = vec![];
    native::enum_windows(|h| {
        let visible = unsafe { IsWindowVisible(h) }.as_bool() && !native::cloaked(h) && native::owner(h).is_invalid();
        if visible
            && style(h, GWL_EXSTYLE) & WS_EX_TOOLWINDOW.0 as i64 == 0
            && !native::title(h).is_empty()
            && !NOT_APPS.contains(&native::class_name(h).as_str())
        {
            list.push(h);
        }
        true
    });
    list
}

pub struct Snapshotter {
    uia: IUIAutomation2,
    cache: IUIAutomationCacheRequest,
    ui_access: bool,
    ids: HashMap<String, u32>,
    next_id: u32,
    seq: u64,
}

struct Ctx<'a> {
    w: &'a Window,
    index: u32,
    found: Vec<Raw>,
    keys: HashSet<String>,
    sparse_document: bool,
}

impl Snapshotter {
    pub fn new(uia: IUIAutomation2, ui_access: bool) -> windows::core::Result<Snapshotter> {
        let cache = unsafe { uia.CreateCacheRequest() }?;
        unsafe {
            for p in PROPS {
                cache.AddProperty(*p)?;
            }
            cache.SetTreeScope(TreeScope_Subtree)?;
            let onscreen = uia.CreatePropertyCondition(UIA_IsOffscreenPropertyId, &VARIANT::from(false))?;
            cache.SetTreeFilter(&uia.CreateAndCondition(&uia.ControlViewCondition()?, &onscreen)?)?;
        }
        Ok(Snapshotter { uia, cache, ui_access, ids: HashMap::new(), next_id: 1, seq: 0 })
    }

    pub fn take(&mut self) -> Snapshot {
        let locked = native::session_locked();
        self.seq += 1;
        let mut snap = Snapshot {
            seq: self.seq,
            guest_time_ms: SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64),
            screen_width: unsafe { GetSystemMetrics(SM_CXSCREEN) },
            screen_height: unsafe { GetSystemMetrics(SM_CYSCREEN) },
            dpi: unsafe { GetDpiForSystem() } as i32,
            user: std::env::var("USERNAME").unwrap_or_default(),
            locked,
            secure_desktop: !locked && !native::input_desktop_is_default(),
            ..Default::default()
        };
        let mut ci = CURSORINFO { cbSize: size_of::<CURSORINFO>() as u32, ..Default::default() };
        if unsafe { GetCursorInfo(&mut ci) }.is_ok() {
            let POINT { x, y } = ci.ptScreenPos;
            (snap.cursor_x, snap.cursor_y) = (x, y);
            let cursor = |id| unsafe { LoadCursorW(None, id) }.map(|c| c.0).unwrap_or(std::ptr::null_mut());
            snap.busy_cursor = ci.hCursor.0 == cursor(IDC_WAIT) || ci.hCursor.0 == cursor(IDC_APPSTARTING);
        }
        if locked {
            return snap;
        }

        for h in app_windows() {
            snap.apps.push(self.describe_window(h, Kind::Unspecified));
        }
        let mut raws: Vec<Raw> = vec![];
        let mut keys = HashSet::new();
        for (hwnd, kind) in collect_windows() {
            let mut w = self.describe_window(hwnd, kind);
            let index = snap.windows.len() as u32;
            if !w.unresponsive && !w.elevated_unreadable {
                watchdog::step(&format!("read {} ({}) {kind:?}", w.class_name, w.process_name));
                let found = self.read_window(hwnd, &mut w, index, &mut keys);
                if found.iter().all(|r| r.e.role == "title bar" || r.e.ancestor_path.starts_with("title bar")) {
                    w.no_tree = true;
                }
                raws.extend(found);
            }
            snap.windows.push(w);
        }
        snap.indeterminate_progress = raws.iter().any(|r| r.busy);
        snap.context = raws.iter().filter(|r| !r.context.is_empty()).take(60).map(|r| r.context.clone()).collect();

        let actionable: Vec<usize> = (0..raws.len()).filter(|&k| raws[k].actionable).collect();
        let mut ordered: Vec<usize> = actionable.iter().copied().filter(|&k| raws[k].e.focused).collect();
        let overlay = snap.windows.iter().any(|w| w.modal || w.kind() == Kind::Popup);
        let of = |kinds: &[Kind]| actionable.iter().copied().filter(|&k| kinds.contains(&raws[k].kind)).collect::<Vec<_>>();
        if overlay {
            ordered.extend(of(&[Kind::Popup, Kind::Owned]));
        } else {
            ordered.extend(of(&[Kind::Foreground, Kind::Owned]));
            ordered.extend(of(&[Kind::Shell]));
        }
        let mut seen = HashSet::new();
        ordered.retain(|k| seen.insert(*k));
        snap.dropped_elements = actionable.len().saturating_sub(ordered.len().min(CAP)) as u32;
        let mut raws: Vec<Option<Raw>> = raws.into_iter().map(Some).collect();
        snap.elements = ordered.into_iter().take(CAP).filter_map(|k| raws[k].take()).map(|r| r.e).collect();
        snap
    }

    fn describe_window(&self, h: HWND, kind: Kind) -> Window {
        let pid = native::pid(h);
        let mut w = Window {
            handle: h.0 as i64,
            title: native::title(h),
            class_name: native::class_name(h),
            process_name: native::process_name(pid),
            pid,
            bounds: Some(native::rect(h).proto()),
            unresponsive: unsafe { IsHungAppWindow(h) }.as_bool(),
            ..Default::default()
        };
        w.set_kind(kind);
        if kind == Kind::Owned {
            let o = native::owner(h);
            w.modal = !o.is_invalid() && !unsafe { IsWindowEnabled(o) }.as_bool();
        }
        if !self.ui_access && native::elevated(pid) {
            w.elevated_unreadable = true;
        }
        w
    }

    fn read_window(&mut self, h: HWND, w: &mut Window, index: u32, keys: &mut HashSet<String>) -> Vec<Raw> {
        let mut attempt = 0;
        loop {
            if java::is_java(h) {
                return self.read_java(h, w, index, keys);
            }
            // A retry starts from the same keys, so its elements keep the keys the first pass would have given.
            let mut cx = Ctx { w, index, found: vec![], keys: keys.clone(), sparse_document: false };
            let r = unsafe { self.uia.ElementFromHandle(h).and_then(|e| e.BuildUpdatedCache(&self.cache)) }.and_then(|root| {
                let framework = s(&root, UIA_FrameworkIdPropertyId);
                let clip = R { x: cx.w.bounds.unwrap_or_default().x, ..rect_of(cx.w) };
                self.walk(&root, "", clip, false, None, &mut cx).map(|()| framework)
            });
            let (found, sparse, new_keys) = (cx.found, cx.sparse_document, cx.keys);
            match r {
                Ok(framework) => {
                    w.framework_id = framework;
                    if sparse && attempt == 0 {
                        attempt += 1;
                        std::thread::sleep(std::time::Duration::from_millis(200));
                        continue;
                    }
                }
                Err(e) if e.code() == UIA_E_TIMEOUT => w.unresponsive = true,
                Err(e) if e.code() == UIA_E_ELEMENTNOTAVAILABLE || e.code() == E_INVALIDARG => {}
                Err(e) if e.code() == E_ACCESSDENIED => w.elevated_unreadable = true,
                Err(_) => w.elevated_unreadable = true,
            }
            *keys = new_keys;
            return found;
        }
    }

    fn walk(&mut self, e: &El, path: &str, clip: R, in_list: bool, content: Option<R>, cx: &mut Ctx) -> windows::core::Result<()> {
        let mut last_text = String::new();
        for (sibling, c) in kids(e)?.iter().enumerate() {
            let role = role(c);
            let row = in_list || (content.is_some() && role != "header");
            let row_clip = match content {
                Some(v) if role != "header" => v,
                _ => clip,
            };
            let raw = self.describe(c, &role, path, row_clip, row, &last_text, sibling, cx)?;
            let mut child_clip = row_clip;
            if role == "document" && s(c, UIA_FrameworkIdPropertyId) == "Chrome" {
                child_clip = rect(raw.e.bounds);
                if kids(c)?.is_empty() {
                    cx.sparse_document = true;
                }
            }
            if role == "text" && !raw.e.name.is_empty() {
                last_text = raw.e.name.clone();
            }
            let seg = format!("{role}:{}", if raw.e.automation_id.is_empty() { &raw.e.class_name } else { &raw.e.automation_id });
            let child_path = if path.is_empty() { seg } else { format!("{path}/{seg}") };
            let raw_content = raw.content;
            if rect(raw.e.bounds).w > 0 || raw.e.focused {
                cx.found.push(raw);
            }
            self.walk(c, &child_path, child_clip, row, raw_content, cx)?;
        }
        Ok(())
    }

    fn key(&mut self, base: String, sibling: usize, keys: &mut HashSet<String>) -> (String, u32) {
        let mut key = hash(&base);
        if !keys.insert(key.clone()) {
            key = hash(&format!("{key}#{sibling}"));
            keys.insert(key.clone());
        }
        let id = *self.ids.entry(key.clone()).or_insert_with(|| {
            self.next_id += 1;
            self.next_id - 1
        });
        (key, id)
    }

    #[allow(clippy::too_many_arguments)]
    fn describe(&mut self, c: &El, role: &str, path: &str, clip: R, in_list: bool, last_text: &str, sibling: usize, cx: &mut Ctx) -> windows::core::Result<Raw> {
        let mut e = Element {
            role: role.to_string(),
            automation_id: s(c, UIA_AutomationIdPropertyId),
            class_name: s(c, UIA_ClassNamePropertyId),
            framework_id: s(c, UIA_FrameworkIdPropertyId),
            ancestor_path: path.to_string(),
            enabled: b(c, UIA_IsEnabledPropertyId),
            focused: b(c, UIA_HasKeyboardFocusPropertyId),
            keyboard_focusable: b(c, UIA_IsKeyboardFocusablePropertyId),
            password: b(c, UIA_IsPasswordPropertyId),
            window: cx.index,
            bounds: Some(if in_list { visible_row(bounds(c), clip) } else { clip_rect(bounds(c), clip) }.proto()),
            ..Default::default()
        };
        e.name = name_of(c, role, last_text);
        if b(c, UIA_IsInvokePatternAvailablePropertyId) || !s(c, UIA_LegacyIAccessibleDefaultActionPropertyId).is_empty() {
            e.actions.push("invoke".into());
        }
        if b(c, UIA_IsValuePatternAvailablePropertyId) {
            e.read_only = b(c, UIA_ValueIsReadOnlyPropertyId);
            if !e.read_only {
                e.actions.push("value".into());
            }
        }
        for (p, a) in [
            (UIA_IsTogglePatternAvailablePropertyId, "toggle"),
            (UIA_IsSelectionItemPatternAvailablePropertyId, "select"),
            (UIA_IsExpandCollapsePatternAvailablePropertyId, "expand"),
            (UIA_IsScrollPatternAvailablePropertyId, "scroll"),
        ] {
            if b(c, p) {
                e.actions.push(a.into());
            }
        }
        e.value = value_of(c, &e);
        e.selected = b(c, UIA_SelectionItemIsSelectedPropertyId);
        e.expanded = matches!(i(c, UIA_ExpandCollapseExpandCollapseStatePropertyId), 1 | 2);
        (e.key, e.id) = self.key(format!("{role}|{}|{}|{path}", e.automation_id, e.name), sibling, &mut cx.keys);

        let mut content = None;
        let mut rows_shown = 0;
        if LISTS.contains(&role) {
            let list: Vec<(String, R)> = kids(c)?.iter().map(|k| (self::role(k), bounds(k))).collect();
            content = content_viewport(rect(e.bounds), &list);
            if let Some(v) = content {
                rows_shown = visible_rows(&list, v);
            }
        }
        common_hints(&mut e, cx.w.kind(), &cx.w.class_name);
        if i(c, UIA_LegacyIAccessibleStatePropertyId) & 0x100 != 0 {
            e.hints.push("Enter triggers this".into());
        }
        if LISTS.contains(&role)
            && b(c, UIA_ScrollVerticallyScrollablePropertyId)
            && let Some(pct) = f(c, UIA_ScrollVerticalScrollPercentPropertyId)
            && pct < 99.5
        {
            e.hints.push(list_shown(rows_shown, pct));
        }
        let mut actionable = !e.actions.is_empty() || (e.keyboard_focusable && INTERACTIVE.contains(&role));
        if matches!(role, "list" | "tree" | "data grid") {
            actionable = !e.hints.is_empty() || e.focused;
        }
        let actionable = actionable && e.enabled;
        let context = if !actionable && !e.name.is_empty() && matches!(role, "text" | "status bar" | "header" | "title bar" | "group") {
            format!("{role}: {}", trim(&e.name, 160))
        } else {
            String::new()
        };
        let busy = role == "progress bar" && !b(c, UIA_IsRangeValuePatternAvailablePropertyId);
        Ok(Raw { e, kind: cx.w.kind(), actionable, context, busy, content })
    }

    fn read_java(&mut self, h: HWND, w: &mut Window, index: u32, keys: &mut HashSet<String>) -> Vec<Raw> {
        w.framework_id = "Java".into();
        let Some((vm, root)) = java::root(h) else { return vec![] };
        let mut cx = Ctx { w, index, found: vec![], keys: std::mem::take(keys), sparse_document: false };
        let mut budget = 3000;
        if let Some(info) = java::info(vm, root) {
            let kids = java::children(vm, root, info.children_count, false);
            let clip = rect_of(cx.w);
            self.walk_java(vm, kids, "", "", clip, &mut cx, &mut budget);
        }
        java::release(vm, root);
        *keys = cx.keys;
        cx.found
    }

    #[allow(clippy::too_many_arguments)]
    fn walk_java(&mut self, vm: i32, kids: Vec<i64>, parent_role: &str, path: &str, clip: R, cx: &mut Ctx, budget: &mut i32) {
        let mut last_text = String::new();
        for (sibling, c) in kids.into_iter().enumerate() {
            self.java_child(vm, c, sibling, parent_role, path, clip, &mut last_text, cx, budget);
            java::release(vm, c);
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn java_child(&mut self, vm: i32, c: i64, sibling: usize, parent_role: &str, path: &str, clip: R, last_text: &mut String, cx: &mut Ctx, budget: &mut i32) {
        *budget -= 1;
        if *budget < 0 {
            return;
        }
        let Some(info) = java::info(vm, c) else { return };
        let states_text = info.states_en_us();
        let states: HashSet<&str> = states_text.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
        if !states.contains("showing") {
            return;
        }
        let bounds = clip_rect(R::new(info.x, info.y, info.width, info.height), clip);
        let role_en = info.role_en_us();
        let mut role = java_role(&role_en, states.contains("editable"));
        if parent_role == "list" && role == Some("text") {
            role = Some("list item");
        }
        let list_like = matches!(role, Some("list" | "table" | "tree"));
        let grand = if role_en == "scroll bar" { vec![] } else { java::children(vm, c, info.children_count, list_like) };
        if let Some(role) = role {
            let mut raw = self.describe_java(vm, c, &info, role, &states, path, bounds, last_text, sibling, cx);
            if list_like && !grand.is_empty() && (grand.len() as i32) < info.children_count {
                let first = java::info(vm, grand[0]).map_or(1, |a| a.index_in_parent + 1);
                raw.e.hints.push(format!("items {first}–{} shown of {}, list continues", first + grand.len() as i32 - 1, info.children_count));
                raw.actionable = raw.e.enabled;
            }
            if role == "text" && !raw.e.name.is_empty() {
                *last_text = raw.e.name.clone();
            }
            if bounds.w > 0 || raw.e.focused {
                cx.found.push(raw);
            }
        }
        let child_clip = if role_en == "viewport" { bounds } else { clip };
        let child_path = if path.is_empty() { role_en.clone() } else { format!("{path}/{role_en}") };
        self.walk_java(vm, grand, role.unwrap_or(""), &child_path, child_clip, cx, budget);
    }

    #[allow(clippy::too_many_arguments)]
    fn describe_java(&mut self, vm: i32, c: i64, info: &java::ContextInfo, role: &str, st: &HashSet<&str>, path: &str, bounds: R, last_text: &str, sibling: usize, cx: &mut Ctx) -> Raw {
        let role_en = info.role_en_us();
        let mut e = Element {
            role: role.into(),
            class_name: role_en.clone(),
            framework_id: "Java".into(),
            ancestor_path: path.into(),
            enabled: st.contains("enabled"),
            focused: st.contains("focused"),
            keyboard_focusable: st.contains("focusable"),
            password: role_en == "password text",
            selected: st.contains("selected"),
            expanded: st.contains("expanded"),
            window: cx.index,
            bounds: Some(bounds.proto()),
            ..Default::default()
        };
        let name = info.name();
        e.name = trim(
            &if !name.is_empty() {
                name
            } else if matches!(role, "edit" | "combo box" | "spinner") && !last_text.is_empty() {
                last_text.to_string()
            } else {
                info.description()
            },
            200,
        );
        let editable = st.contains("editable");
        if info.accessible_action != 0 && !matches!(role, "edit" | "text" | "list" | "table" | "tree") {
            e.actions.push("invoke".into());
        }
        if role == "edit" {
            e.read_only = !editable;
            if editable {
                e.actions.push("value".into());
            }
        }
        if matches!(role_en.as_str(), "check box" | "toggle button" | "check box menu item") {
            e.actions.push("toggle".into());
        }
        if matches!(role, "radio button" | "list item" | "tab item") {
            e.actions.push("select".into());
        }
        if role == "combo box" || role_en == "menu" {
            e.actions.push("expand".into());
        }
        if e.password {
            e.value = if java::text_length(vm, c) > 0 { "filled" } else { "empty" }.into();
        } else if e.actions.iter().any(|a| a == "toggle") {
            e.value = if st.contains("checked") { "on" } else { "off" }.into();
        } else if matches!(role, "edit" | "combo box") && info.accessible_text != 0 {
            e.value = trim(&java::text(vm, c, MAX_VALUE as i32 + 1), MAX_VALUE);
        }
        (e.key, e.id) = self.key(format!("{role}||{}|{path}", e.name), sibling, &mut cx.keys);
        common_hints(&mut e, cx.w.kind(), &cx.w.class_name);
        let mut actionable = !e.actions.is_empty() || (e.keyboard_focusable && INTERACTIVE.contains(&role));
        if matches!(role, "list" | "tree" | "table") {
            actionable = e.focused;
        }
        let actionable = actionable && e.enabled;
        let context = if !actionable && !e.name.is_empty() && matches!(role, "text" | "status bar") { format!("{role}: {}", trim(&e.name, 160)) } else { String::new() };
        let busy = role == "progress bar" && st.contains("indeterminate");
        Raw { e, kind: cx.w.kind(), actionable, context, busy, content: None }
    }
}

fn rect(r: Option<ludius_proto::v1::Rect>) -> R {
    r.map_or_else(R::default, |r| R::new(r.x, r.y, r.width, r.height))
}

fn rect_of(w: &Window) -> R {
    rect(w.bounds)
}

fn clip_rect(a: R, b: R) -> R {
    clip(a, b)
}

fn name_of(c: &El, role: &str, last_text: &str) -> String {
    let name = s(c, UIA_NamePropertyId);
    if !name.is_empty() {
        return trim(&name, 200);
    }
    if let Ok(l) = unsafe { c.CachedLabeledBy() }
        && let Ok(n) = unsafe { l.CurrentName() }
        && !n.is_empty()
    {
        return trim(&n.to_string(), 200);
    }
    let name = s(c, UIA_LegacyIAccessibleNamePropertyId);
    if !name.is_empty() {
        return trim(&name, 200);
    }
    if matches!(role, "edit" | "combo box" | "spinner") && !last_text.is_empty() {
        return trim(last_text, 200);
    }
    trim(&s(c, UIA_HelpTextPropertyId), 200)
}

fn value_of(c: &El, e: &Element) -> String {
    let v = s(c, UIA_ValueValuePropertyId);
    if e.password {
        return if !v.is_empty() || !s(c, UIA_LegacyIAccessibleValuePropertyId).is_empty() { "filled" } else { "empty" }.into();
    }
    if !v.is_empty() {
        return trim(&v, MAX_VALUE);
    }
    if b(c, UIA_IsTogglePatternAvailablePropertyId) {
        return match i(c, UIA_ToggleToggleStatePropertyId) {
            0 => "off",
            1 => "on",
            _ => "indeterminate",
        }
        .into();
    }
    if b(c, UIA_IsRangeValuePatternAvailablePropertyId)
        && let Some(d) = f(c, UIA_RangeValueValuePropertyId)
    {
        return short_number(d);
    }
    if b(c, UIA_IsExpandCollapsePatternAvailablePropertyId) {
        return if matches!(i(c, UIA_ExpandCollapseExpandCollapseStatePropertyId), 1 | 2) { "expanded" } else { "collapsed" }.into();
    }
    String::new()
}

#[allow(dead_code)]
fn bstr(b: &BSTR) -> String {
    b.to_string()
}
