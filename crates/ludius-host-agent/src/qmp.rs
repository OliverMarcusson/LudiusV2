use crate::error::{E, R, down};
use serde_json::{Value, json};
use std::os::fd::{AsRawFd, RawFd};
use std::path::PathBuf;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt, Interest};
use tokio::net::UnixStream;

const TIMEOUT: Duration = Duration::from_secs(10);

pub struct Qmp {
    pub path: PathBuf,
}

struct Conn {
    s: UnixStream,
    buf: Vec<u8>,
}

impl Qmp {
    pub async fn execute(&self, command: &str, arguments: Option<Value>) -> R<Value> {
        Ok(self.run(&[(command, arguments)], None).await?.remove(0))
    }

    /// A VNC connection to the VM's console: one end of a socket pair handed to QEMU (getfd + add_client).
    pub async fn vnc_client(&self) -> R<UnixStream> {
        let (ours, theirs) = std::os::unix::net::UnixStream::pair().map_err(down)?;
        let name = json!({ "fdname": "ludius-monitor" });
        let add = json!({ "protocol": "vnc", "fdname": "ludius-monitor", "skipauth": true });
        self.run(&[("getfd", Some(name)), ("add_client", Some(add))], Some(theirs.as_raw_fd())).await?;
        drop(theirs);
        ours.set_nonblocking(true).map_err(down)?;
        UnixStream::from_std(ours).map_err(down)
    }

    /// Runs commands on one QMP session; `pass_fd` rides along with the first command (SCM_RIGHTS).
    pub async fn run(&self, commands: &[(&str, Option<Value>)], pass_fd: Option<RawFd>) -> R<Vec<Value>> {
        tokio::time::timeout(TIMEOUT, self.session(commands, pass_fd))
            .await
            .map_err(|_| E::Down(format!("QMP {}: timed out", self.path.display())))?
    }

    async fn session(&self, commands: &[(&str, Option<Value>)], mut pass_fd: Option<RawFd>) -> R<Vec<Value>> {
        let s = UnixStream::connect(&self.path)
            .await
            .map_err(|e| E::Down(format!("QMP socket {}: {e} (VM off?)", self.path.display())))?;
        let mut c = Conn { s, buf: vec![] };
        if c.line().await?.get("QMP").is_none() {
            return Err(down("QMP: no greeting"));
        }
        c.send(&message("qmp_capabilities", None)).await?;
        c.reply().await?;
        let mut out = vec![];
        for (command, arguments) in commands {
            let msg = message(command, arguments.clone());
            match pass_fd.take() {
                Some(fd) => c.send_with_fd(&msg, fd).await?,
                None => c.send(&msg).await?,
            }
            out.push(c.reply().await?);
        }
        Ok(out)
    }
}

fn message(command: &str, arguments: Option<Value>) -> Vec<u8> {
    let mut o = json!({ "execute": command });
    if let Some(a) = arguments {
        o["arguments"] = a;
    }
    let mut b = serde_json::to_vec(&o).expect("json");
    b.push(b'\n');
    b
}

impl Conn {
    async fn send(&mut self, b: &[u8]) -> R<()> {
        self.s.write_all(b).await.map_err(down)
    }

    async fn send_with_fd(&mut self, b: &[u8], fd: RawFd) -> R<()> {
        loop {
            self.s.writable().await.map_err(down)?;
            match self.s.try_io(Interest::WRITABLE, || sendmsg_fd(self.s.as_raw_fd(), b, fd)) {
                Ok(()) => return Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
                Err(e) => return Err(E::Down(format!("QMP sendmsg: {e}"))),
            }
        }
    }

    async fn line(&mut self) -> R<Value> {
        loop {
            if let Some(i) = self.buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = self.buf.drain(..=i).collect();
                return match serde_json::from_slice::<Value>(&line) {
                    Ok(v) if v.is_object() => Ok(v),
                    _ => Err(down("QMP: not a JSON object")),
                };
            }
            let mut chunk = [0u8; 8192];
            let n = self.s.read(&mut chunk).await.map_err(down)?;
            if n == 0 {
                return Err(down("QMP: connection closed"));
            }
            self.buf.extend_from_slice(&chunk[..n]);
        }
    }

    async fn reply(&mut self) -> R<Value> {
        loop {
            let mut o = self.line().await?;
            if let Some(r) = o.get_mut("return") {
                return Ok(r.take());
            }
            if let Some(e) = o.get("error") {
                return Err(E::Failed(format!("QMP {}: {}", e["class"], e["desc"])));
            }
        }
    }
}

fn sendmsg_fd(sock: RawFd, data: &[u8], fd: RawFd) -> std::io::Result<()> {
    // SAFETY: msghdr points at locals that outlive the call; the control buffer is CMSG_SPACE bytes for one fd.
    unsafe {
        let mut iov = libc::iovec { iov_base: data.as_ptr() as *mut _, iov_len: data.len() };
        let space = libc::CMSG_SPACE(size_of::<RawFd>() as u32) as usize;
        let mut control = [0u64; 4]; // u64 keeps the cmsghdr aligned; 32 bytes >= CMSG_SPACE(one fd)
        debug_assert!(space <= size_of_val(&control));
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr() as *mut _;
        msg.msg_controllen = space as _;
        let c = libc::CMSG_FIRSTHDR(&msg);
        (*c).cmsg_level = libc::SOL_SOCKET;
        (*c).cmsg_type = libc::SCM_RIGHTS;
        (*c).cmsg_len = libc::CMSG_LEN(size_of::<RawFd>() as u32) as _;
        std::ptr::write_unaligned(libc::CMSG_DATA(c) as *mut RawFd, fd);
        let n = libc::sendmsg(sock, &msg, libc::MSG_NOSIGNAL);
        if n < 0 {
            return Err(std::io::Error::last_os_error());
        }
        if n as usize != data.len() {
            return Err(std::io::Error::other("short write"));
        }
    }
    Ok(())
}

pub fn abs(axis: &str, value: i32) -> Value {
    json!({ "type": "abs", "data": { "axis": axis, "value": value } })
}

pub fn button(button: &str, down: bool) -> Value {
    json!({ "type": "btn", "data": { "down": down, "button": button } })
}

pub fn key(qcode: &str, down: bool) -> Value {
    json!({ "type": "key", "data": { "down": down, "key": { "type": "qcode", "data": qcode } } })
}

/// Windows virtual-key code to QEMU qcode.
pub fn qcode(vk: u32) -> R<String> {
    let fixed = match vk {
        0x08 => "backspace",
        0x09 => "tab",
        0x0D => "ret",
        0x10 | 0xA0 => "shift",
        0x11 | 0xA2 => "ctrl",
        0x12 | 0xA4 => "alt",
        0x13 => "pause",
        0x14 => "caps_lock",
        0x1B => "esc",
        0x20 => "spc",
        0x21 => "pgup",
        0x22 => "pgdn",
        0x23 => "end",
        0x24 => "home",
        0x25 => "left",
        0x26 => "up",
        0x27 => "right",
        0x28 => "down",
        0x2C => "print",
        0x2D => "insert",
        0x2E => "delete",
        0x5B => "meta_l",
        0x5C => "meta_r",
        0x5D => "menu",
        0x6A => "kp_multiply",
        0x6B => "kp_add",
        0x6D => "kp_subtract",
        0x6E => "kp_decimal",
        0x6F => "kp_divide",
        0x90 => "num_lock",
        0x91 => "scroll_lock",
        0xA1 => "shift_r",
        0xA3 => "ctrl_r",
        0xA5 => "alt_r",
        0xBA => "semicolon",
        0xBB => "equal",
        0xBC => "comma",
        0xBD => "minus",
        0xBE => "dot",
        0xBF => "slash",
        0xC0 => "grave_accent",
        0xDB => "bracket_left",
        0xDC => "backslash",
        0xDD => "bracket_right",
        0xDE => "apostrophe",
        _ => "",
    };
    Ok(match vk {
        _ if !fixed.is_empty() => fixed.to_string(),
        0x41..=0x5A => char::from_u32(vk + 32).unwrap().to_string(),
        0x30..=0x39 => char::from_u32(vk).unwrap().to_string(),
        0x70..=0x7B => format!("f{}", vk - 0x70 + 1),
        0x60..=0x69 => format!("kp_{}", vk - 0x60),
        _ => return Err(E::Arg(format!("no key for virtual-key 0x{vk:02X}"))),
    })
}

const UNSHIFTED: &str = "`1234567890-=[]\\;',./";
const SHIFTED: &str = "~!@#$%^&*()_+{}|:\"<>?";

/// The qcode and shift state that types `c` on a US layout.
pub fn for_char(c: char) -> R<(String, bool)> {
    let oem = |u: char| -> u32 {
        match u {
            '0'..='9' => u as u32,
            '`' => 0xC0,
            '-' => 0xBD,
            '=' => 0xBB,
            '[' => 0xDB,
            ']' => 0xDD,
            '\\' => 0xDC,
            ';' => 0xBA,
            '\'' => 0xDE,
            ',' => 0xBC,
            '.' => 0xBE,
            _ => 0xBF,
        }
    };
    Ok(match c {
        '\n' => ("ret".into(), false),
        '\t' => ("tab".into(), false),
        ' ' => ("spc".into(), false),
        'a'..='z' => (c.to_string(), false),
        'A'..='Z' => (c.to_ascii_lowercase().to_string(), true),
        _ => {
            if UNSHIFTED.contains(c) {
                (qcode(oem(c))?, false)
            } else if let Some(i) = SHIFTED.chars().position(|s| s == c) {
                (qcode(oem(UNSHIFTED.chars().nth(i).unwrap()))?, true)
            } else {
                return Err(E::Arg("only printable ASCII, \\n and \\t can be typed".into()));
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys() {
        assert_eq!(qcode(0x41).unwrap(), "a");
        assert_eq!(qcode(0x35).unwrap(), "5");
        assert_eq!(qcode(0x7B).unwrap(), "f12");
        assert_eq!(qcode(0x63).unwrap(), "kp_3");
        assert!(qcode(0xFF).is_err());
        assert_eq!(for_char('Q').unwrap(), ("q".into(), true));
        assert_eq!(for_char('/').unwrap(), ("slash".into(), false));
        assert_eq!(for_char('?').unwrap(), ("slash".into(), true));
        assert_eq!(for_char('@').unwrap(), ("2".into(), true));
        assert_eq!(for_char('"').unwrap(), ("apostrophe".into(), true));
        assert!(for_char('é').is_err());
    }
}
