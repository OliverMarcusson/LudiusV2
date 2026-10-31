//! Guest observer: serves UIA snapshots and noise-folder file reads to the host over Hyper-V sockets or vsock.
#![cfg_attr(all(windows, not(test)), windows_subsystem = "windows")]

#[cfg_attr(not(windows), allow(dead_code))]
mod lists;
#[cfg(windows)]
mod files;
#[cfg(windows)]
mod java;
#[cfg(windows)]
mod native;
#[cfg(windows)]
mod settle;
#[cfg(windows)]
mod snapshot;
#[cfg(windows)]
mod watchdog;

#[cfg(not(windows))]
fn main() {
    eprintln!("uiprobe runs on Windows only");
    std::process::exit(2);
}

#[cfg(windows)]
fn main() {
    std::process::exit(win::run());
}

#[cfg(windows)]
mod win {
    use crate::{files, java, native, settle::Settle, snapshot, watchdog};
    use ludius_proto::v1::{Hello, ObserverRequest, ObserverResponse, Snapshot, SnapshotRequest, observer_request, observer_response};
    use prost::Message;
    use std::io::{Read, Write};
    use windows::Win32::Foundation::{HANDLE, WAIT_ABANDONED, WAIT_OBJECT_0};
    use windows::Win32::Networking::WinSock::*;
    use windows::Win32::System::Com::{CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED, CoCreateInstance, CoInitializeEx};
    use windows::Win32::System::Threading::{CreateEventW, CreateMutexW, INFINITE, ResetEvent, SetEvent, WaitForSingleObject};
    use windows::Win32::UI::Accessibility::{CUIAutomation8, IUIAutomation2};
    use windows::Win32::UI::WindowsAndMessaging::FindWindowW;
    use windows::core::{HSTRING, PCWSTR};

    const VERSION: &str = if cfg!(feature = "watchdog-test") { concat!(env!("CARGO_PKG_VERSION"), "-watchdogtest") } else { env!("CARGO_PKG_VERSION") };
    const SERVICE: &str = "000011cb-facb-11e6-bd58-64006a7986d3";
    const MAX_FRAME: usize = 16 << 20;

    fn arg(name: &str) -> Option<String> {
        let a: Vec<String> = std::env::args().collect();
        a.iter().position(|x| x == name).and_then(|i| a.get(i + 1).cloned())
    }

    fn flag(name: &str) -> bool {
        std::env::args().any(|x| x == name)
    }

    /// GUID text to its in-memory layout (Data1..3 little-endian).
    fn guid(s: &str) -> Option<[u8; 16]> {
        let h = s.replace('-', "");
        if h.len() != 32 {
            return None;
        }
        let b: Vec<u8> = (0..16).map(|i| u8::from_str_radix(&h[2 * i..2 * i + 2], 16)).collect::<Result<_, _>>().ok()?;
        let mut g = [0u8; 16];
        g[..4].copy_from_slice(&[b[3], b[2], b[1], b[0]]);
        g[4..8].copy_from_slice(&[b[5], b[4], b[7], b[6]]);
        g[8..].copy_from_slice(&b[8..]);
        Some(g)
    }

    struct Sock(SOCKET);
    impl Drop for Sock {
        fn drop(&mut self) {
            unsafe { closesocket(self.0) };
        }
    }
    fn wsa_err(what: &str) -> std::io::Error {
        std::io::Error::other(format!("{what}: WSA error {}", unsafe { WSAGetLastError() }.0))
    }
    impl Read for Sock {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            let n = unsafe { recv(self.0, buf, SEND_RECV_FLAGS(0)) };
            if n < 0 { Err(wsa_err("recv")) } else { Ok(n as usize) }
        }
    }
    impl Write for Sock {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            let n = unsafe { send(self.0, buf, SEND_RECV_FLAGS(0)) };
            if n < 0 { Err(wsa_err("send")) } else { Ok(n as usize) }
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// One listener for both transports: the address family, protocol and raw sockaddr differ.
    fn listen_on(family: u16, protocol: i32, addr: &[u8]) -> std::io::Result<Sock> {
        let mut wsa = WSADATA::default();
        if unsafe { WSAStartup(0x0202, &mut wsa) } != 0 {
            return Err(wsa_err("WSAStartup"));
        }
        let s = unsafe { WSASocketW(family as i32, SOCK_STREAM.0, protocol, None, 0, 0) }.map_err(|_| wsa_err("socket"))?;
        let s = Sock(s);
        if unsafe { bind(s.0, addr.as_ptr().cast(), addr.len() as i32) } != 0 || unsafe { listen(s.0, 1) } != 0 {
            return Err(wsa_err("bind"));
        }
        Ok(s)
    }

    fn send_msg(w: &mut impl Write, m: &ObserverResponse) -> std::io::Result<()> {
        let body = m.encode_to_vec();
        w.write_all(&(body.len() as u32).to_le_bytes())?;
        w.write_all(&body)
    }

    fn read_frame(r: &mut impl Read) -> std::io::Result<Vec<u8>> {
        let mut len = [0u8; 4];
        r.read_exact(&mut len)?;
        let n = u32::from_le_bytes(len) as usize;
        if n > MAX_FRAME {
            return Err(std::io::Error::other("frame too large"));
        }
        let mut body = vec![0u8; n];
        r.read_exact(&mut body)?;
        Ok(body)
    }

    struct Observer {
        settle: Settle,
        snapper: snapshot::Snapshotter,
        ui_access: bool,
        session: u32,
    }

    fn env(name: &str) -> String {
        std::env::var(name).unwrap_or_default()
    }

    impl Observer {
        fn hello(&self, full: bool) -> Hello {
            Hello {
                version: VERSION.into(),
                user: env("USERNAME"),
                session_id: if full { self.session } else { 0 },
                ui_access: self.ui_access,
                pixel_settle: self.settle.pixel_settle(),
                computer: if full { env("COMPUTERNAME") } else { String::new() },
            }
        }

        fn observe(&mut self, r: &SnapshotRequest) -> Snapshot {
            #[cfg(feature = "watchdog-test")]
            {
                if r.quiet_ms == 4242 {
                    loop {
                        std::thread::park();
                    }
                }
                if r.quiet_ms == 4243 {
                    panic!("injected watchdog test failure");
                }
            }
            let (settled, ms) = if r.wait_settle {
                self.settle.wait(if r.quiet_ms == 0 { 300 } else { r.quiet_ms }, if r.timeout_ms == 0 { 5000 } else { r.timeout_ms })
            } else {
                (true, 0)
            };
            let mut snap = self.snapper.take();
            snap.settled = settled;
            snap.settle_ms = ms;
            snap
        }

        fn handle(&mut self, req: ObserverRequest) -> ObserverResponse {
            use observer_request::Body as Q;
            use observer_response::Body as A;
            watchdog::begin();
            watchdog::step("request");
            let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match &req.body {
                Some(Q::Snapshot(s)) => Ok(A::Snapshot(self.observe(s))),
                Some(Q::ReadFile(f)) => files::read(f, std::path::Path::new(&env("USERPROFILE"))).map(A::File),
                Some(Q::Ping(_)) => Ok(A::Hello(self.hello(false))),
                None => Err("unknown request".to_string()),
            }));
            let body = match r {
                Ok(Ok(b)) => b,
                Ok(Err(e)) => A::Error(e),
                Err(p) => A::Error(format!("panic: {}", p.downcast_ref::<&str>().copied().or(p.downcast_ref::<String>().map(String::as_str)).unwrap_or("?"))),
            };
            watchdog::end(!matches!(body, A::Error(_)));
            ObserverResponse { id: req.id, body: Some(body) }
        }

        /// One host connection: Hello, then request/response frames until the host hangs up.
        /// A malformed frame drops the connection; the host reconnects.
        fn serve(&mut self, mut c: Sock) {
            if send_msg(&mut c, &ObserverResponse { id: 0, body: Some(observer_response::Body::Hello(self.hello(true))) }).is_err() {
                return;
            }
            while let Ok(frame) = read_frame(&mut c) {
                let Ok(req) = ObserverRequest::decode(frame.as_slice()) else { return };
                let res = self.handle(req);
                if send_msg(&mut c, &res).is_err() {
                    return;
                }
            }
        }
    }

    struct H(HANDLE);
    unsafe impl Send for H {}

    pub fn run() -> i32 {
        let once = flag("--once");
        let session = native::own_session();
        if session == 0 && !once {
            return 2;
        }
        let Some(service) = guid(&arg("--service").unwrap_or(SERVICE.into())) else { return 2 };

        // A new instance asks the old one to quit once idle, then takes the single-instance mutex.
        let (quit, single) = unsafe {
            (CreateEventW(None, false, false, &HSTRING::from(r"Local\RangeAssistQuit")), CreateMutexW(None, false, &HSTRING::from(r"Local\RangeAssist")))
        };
        let (Ok(quit), Ok(single)) = (quit, single) else { return 1 };
        if !once {
            unsafe {
                let _ = SetEvent(quit);
                let w = WaitForSingleObject(single, 150_000);
                if w != WAIT_OBJECT_0 && w != WAIT_ABANDONED {
                    return 1;
                }
                let _ = ResetEvent(quit);
            }
            let q = H(quit);
            std::thread::spawn(move || {
                let q = q;
                unsafe { WaitForSingleObject(q.0, INFINITE) };
                for _ in 0..480 {
                    if watchdog::idle(10_000) {
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(250));
                }
                std::process::exit(0);
            });
            watchdog::spawn();
        }

        let uia: IUIAutomation2 = match unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok().and_then(|()| CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER))
        } {
            Ok(u) => u,
            Err(_) => return 1,
        };
        unsafe {
            let _ = uia.SetConnectionTimeout(2000);
            let _ = uia.SetTransactionTimeout(3000);
        }
        let ui_access = native::self_ui_access();
        java::start();
        watchdog::begin();
        watchdog::step("startup: settle handlers");
        let Ok(settle) = Settle::new(uia.clone()) else { return 1 };
        watchdog::end(true);
        let Ok(snapper) = snapshot::Snapshotter::new(uia, ui_access) else { return 1 };
        let mut obs = Observer { settle, snapper, ui_access, session };

        if once {
            if let Some(t) = arg("--title") {
                let h = unsafe { FindWindowW(PCWSTR::null(), &HSTRING::from(t)) }.unwrap_or_default();
                snapshot::FOREGROUND.store(h.0 as isize, std::sync::atomic::Ordering::Relaxed);
            }
            println!("{:#?}", obs.observe(&SnapshotRequest { wait_settle: true, ..Default::default() }));
            return 0;
        }

        let listener = match arg("--transport").as_deref() {
            None | Some("hyperv") => {
                // SOCKADDR_HV: family, reserved, VmId (wildcard), ServiceId.
                let mut a = [0u8; 36];
                a[..2].copy_from_slice(&34u16.to_le_bytes());
                a[20..].copy_from_slice(&service);
                listen_on(34, 1, &a)
            }
            Some("vsock") => {
                let Ok(family) = arg("--vsock-family").unwrap_or("40".into()).parse::<u16>() else { return 2 };
                // sockaddr_vm: family, reserved, port (the service GUID's Data1), cid (any).
                let mut a = [0u8; 16];
                a[..2].copy_from_slice(&family.to_le_bytes());
                a[4..8].copy_from_slice(&service[..4]);
                a[8..12].copy_from_slice(&u32::MAX.to_le_bytes());
                listen_on(family, 0, &a)
            }
            Some(_) => return 2,
        };
        let Ok(listener) = listener else { return 1 };
        loop {
            let c = unsafe { accept(listener.0, None, None) };
            match c {
                Ok(c) => obs.serve(Sock(c)),
                Err(_) => std::thread::sleep(std::time::Duration::from_millis(100)),
            }
        }
    }

    #[cfg(test)]
    mod tests {
        #[test]
        fn guid_layout() {
            let g = super::guid(super::SERVICE).unwrap();
            assert_eq!(u32::from_le_bytes(g[..4].try_into().unwrap()), 4555);
            assert_eq!(&g[4..8], &[0xcb, 0xfa, 0xe6, 0x11]);
            assert_eq!(&g[8..], &[0xbd, 0x58, 0x64, 0x00, 0x6a, 0x79, 0x86, 0xd3]);
        }
    }
}
