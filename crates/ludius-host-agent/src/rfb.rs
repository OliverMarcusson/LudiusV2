//! Console frames for the monitor: an RFB (VNC) client on a QMP-added socket, raw encoding only,
//! scaled down and JPEG-encoded per request. One capture per VM, stopped when nobody asks for 10 s.
use crate::error::{E, R};
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::watch;

const MAX_SIDE: usize = 8192;
const INTERVAL: Duration = Duration::from_millis(1000 / 15);
const IDLE_AFTER: Duration = Duration::from_secs(10);
const FIRST_FRAME: Duration = Duration::from_secs(3);
const HANDSHAKE: Duration = Duration::from_secs(10);
const PROBE: Duration = Duration::from_secs(2);
const STALL: Duration = Duration::from_secs(5);
const QUALITY: u8 = 80;

pub type Connect = Pin<Box<dyn Future<Output = R<UnixStream>> + Send>>;

/// Frame version and its JPEG, per max width.
type Encoded = (u64, Arc<Vec<u8>>);

#[derive(Clone, PartialEq)]
enum State {
    Starting,
    Ready,
    Ended(String),
}

#[derive(Default)]
struct Fb {
    w: usize,
    h: usize,
    pixels: Vec<u8>,
    version: u64,
}

struct Capture {
    tag: String,
    state: watch::Sender<State>,
    asked_at: Mutex<Instant>,
    heard_at: Mutex<Instant>,
    fb: Mutex<Fb>,
    encoded: Mutex<HashMap<usize, Encoded>>,
}

#[derive(Default)]
pub struct Captures {
    live: Arc<Mutex<HashMap<String, Arc<Capture>>>>,
}

impl Captures {
    pub async fn frame(&self, vm: &str, tag: String, connect: impl FnOnce() -> Connect, max_width: usize) -> R<Arc<Vec<u8>>> {
        let c = {
            let mut live = self.live.lock().unwrap();
            let current = live.get(vm).filter(|c| c.tag == tag).cloned();
            if let Some(State::Ended(e)) = current.as_ref().map(|c| c.state.borrow().clone()) {
                live.remove(vm);
                return Err(E::Down(e));
            }
            let c = match current {
                Some(c) => c,
                None => {
                    if let Some(old) = live.remove(vm) {
                        old.state.send_replace(State::Ended("console capture ended".into()));
                    }
                    let c = Arc::new(Capture {
                        tag,
                        state: watch::channel(State::Starting).0,
                        asked_at: Mutex::new(Instant::now()),
                        heard_at: Mutex::new(Instant::now()),
                        fb: Mutex::default(),
                        encoded: Mutex::default(),
                    });
                    live.insert(vm.into(), c.clone());
                    tokio::spawn(supervise(self.live.clone(), vm.to_string(), c.clone(), connect()));
                    c
                }
            };
            *c.asked_at.lock().unwrap() = Instant::now();
            c
        };
        let mut rx = c.state.subscribe();
        match tokio::time::timeout(FIRST_FRAME, rx.wait_for(|s| *s != State::Starting)).await {
            Err(_) => return Err(E::Down("no console frame yet".into())),
            Ok(Err(_)) => return Err(E::Down("console capture ended".into())),
            Ok(Ok(s)) => {
                if let State::Ended(e) = &*s {
                    return Err(E::Down(e.clone()));
                }
            }
        }
        tokio::task::spawn_blocking(move || encode(&c, max_width)).await.map_err(|e| E::Failed(e.to_string()))?
    }

    pub fn close(&self, vm: &str) {
        if let Some(c) = self.live.lock().unwrap().remove(vm) {
            c.state.send_replace(State::Ended("console capture ended".into()));
        }
    }
}

/// Runs one capture and stops it when it ends, stalls, or nobody asked for a frame lately.
async fn supervise(live: Arc<Mutex<HashMap<String, Arc<Capture>>>>, vm: String, c: Arc<Capture>, connect: Connect) {
    let started = Instant::now();
    let mut work = tokio::spawn(run(c.clone(), connect));
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    let error = loop {
        tokio::select! {
            r = &mut work => break match r {
                Ok(Err(e)) => Some(e.to_string()),
                _ => None,
            },
            _ = tick.tick() => {
                let ready = *c.state.borrow() == State::Ready;
                let error = match &*c.state.borrow() {
                    State::Ended(_) => Some(None),
                    _ if c.asked_at.lock().unwrap().elapsed() >= IDLE_AFTER => Some(None),
                    _ if ready && c.heard_at.lock().unwrap().elapsed() > STALL => Some(Some("capture stalled".to_string())),
                    _ if !ready && started.elapsed() > HANDSHAKE => Some(Some("console capture did not start".to_string())),
                    _ => None,
                };
                if let Some(e) = error {
                    work.abort();
                    break e;
                }
            }
        }
    };
    let failed = error.is_some();
    c.state.send_if_modified(|s| {
        if matches!(s, State::Ended(_)) {
            return false;
        }
        *s = State::Ended(error.unwrap_or_else(|| "console capture ended".into()));
        true
    });
    let mut live = live.lock().unwrap();
    // A failed capture stays listed so the next frame request reports why, then starts afresh.
    if !failed && live.get(&vm).is_some_and(|now| Arc::ptr_eq(now, &c)) {
        live.remove(&vm);
    }
}

fn rfb(m: impl Into<String>) -> E {
    E::Down(format!("RFB: {}", m.into()))
}

async fn run(c: Arc<Capture>, connect: Connect) -> R<()> {
    let mut s = BufReader::new(connect.await?);
    let version = bytes(&mut s, 12).await?;
    let v = String::from_utf8_lossy(&version);
    if !v.starts_with("RFB 003.") || !v[8..11].parse::<u32>().is_ok_and(|minor| minor >= 8) {
        return Err(rfb(format!("unsupported server version {}", v.trim())));
    }
    send(&mut s, b"RFB 003.008\n").await?;
    let n = bytes(&mut s, 1).await?[0];
    if n == 0 {
        return Err(rfb(format!("refused: {}", reason(&mut s).await?)));
    }
    if !bytes(&mut s, n as usize).await?.contains(&1) {
        return Err(rfb("server offers no security type None"));
    }
    send(&mut s, &[1]).await?;
    if u32::from_be_bytes(bytes(&mut s, 4).await?.try_into().unwrap()) != 0 {
        return Err(rfb(format!("security failed: {}", reason(&mut s).await?)));
    }
    send(&mut s, &[1]).await?;
    let init = bytes(&mut s, 24).await?;
    let name_len = u32::from_be_bytes(init[20..24].try_into().unwrap()) as usize;
    if name_len > 4096 {
        return Err(rfb("desktop name too long"));
    }
    bytes(&mut s, name_len).await?;
    resize(&c, be16(&init[0..2]), be16(&init[2..4]))?;
    // 32 bpp, depth 24, little endian, true colour, R/G/B shifts 16/8/0: pixels arrive as B,G,R,X.
    send(&mut s, &[0, 0, 0, 0, 32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0]).await?;
    // Raw, plus DesktopSize (-223).
    send(&mut s, &[2, 0, 0, 2, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0x21]).await?;

    let mut rect = Vec::new();
    let mut asked = Instant::now();
    update_request(&mut s, &c, false).await?;
    loop {
        // fill_buf is cancel-safe: no message is half-read when the probe fires.
        match tokio::time::timeout(PROBE, s.fill_buf()).await {
            Err(_) => {
                update_request(&mut s, &c, false).await?;
                continue;
            }
            Ok(Ok([])) => return Err(rfb("connection closed")),
            Ok(r) => r.map_err(|e| rfb(e.to_string()))?,
        };
        match bytes(&mut s, 1).await?[0] {
            0 => {
                let resized = update(&mut s, &c, &mut rect).await?;
                *c.heard_at.lock().unwrap() = Instant::now();
                c.state.send_if_modified(|st| {
                    let starting = *st == State::Starting;
                    if starting {
                        *st = State::Ready;
                    }
                    starting
                });
                if let Some(wait) = INTERVAL.checked_sub(asked.elapsed()) {
                    tokio::time::sleep(wait).await;
                }
                asked = Instant::now();
                update_request(&mut s, &c, !resized).await?;
            }
            1 => {
                let map = bytes(&mut s, 5).await?;
                bytes(&mut s, be16(&map[3..5]) * 6).await?;
            }
            2 => {}
            3 => {
                let cut = bytes(&mut s, 7).await?;
                let len = u32::from_be_bytes(cut[3..7].try_into().unwrap()) as usize;
                if len > 1 << 20 {
                    return Err(rfb("cut text too long"));
                }
                bytes(&mut s, len).await?;
            }
            t => return Err(rfb(format!("unexpected server message {t}"))),
        }
    }
}

async fn update(s: &mut BufReader<UnixStream>, c: &Capture, rect: &mut Vec<u8>) -> R<bool> {
    let n = be16(&bytes(s, 3).await?[1..3]);
    let mut resized = false;
    for _ in 0..n {
        let h = bytes(s, 12).await?;
        let (x, y, w, hh) = (be16(&h[0..2]), be16(&h[2..4]), be16(&h[4..6]), be16(&h[6..8]));
        match i32::from_be_bytes(h[8..12].try_into().unwrap()) {
            -223 => {
                resize(c, w, hh)?;
                resized = true;
            }
            0 => {
                let (fw, fh) = {
                    let fb = c.fb.lock().unwrap();
                    (fb.w, fb.h)
                };
                if x + w > fw || y + hh > fh {
                    return Err(rfb("rectangle outside the framebuffer"));
                }
                rect.resize(w * hh * 4, 0);
                s.read_exact(rect).await.map_err(|e| rfb(e.to_string()))?;
                let mut fb = c.fb.lock().unwrap();
                if fb.w != fw {
                    return Err(rfb("framebuffer changed mid-update"));
                }
                for row in 0..hh {
                    let to = ((y + row) * fw + x) * 4;
                    fb.pixels[to..to + w * 4].copy_from_slice(&rect[row * w * 4..(row + 1) * w * 4]);
                }
            }
            e => return Err(rfb(format!("unrequested encoding {e}"))),
        }
    }
    c.fb.lock().unwrap().version += 1;
    Ok(resized)
}

fn resize(c: &Capture, w: usize, h: usize) -> R<()> {
    if !(1..=MAX_SIDE).contains(&w) || !(1..=MAX_SIDE).contains(&h) {
        return Err(rfb(format!("framebuffer {w}x{h}")));
    }
    let mut fb = c.fb.lock().unwrap();
    (fb.w, fb.h, fb.pixels) = (w, h, vec![0; w * h * 4]);
    Ok(())
}

async fn update_request(s: &mut BufReader<UnixStream>, c: &Capture, incremental: bool) -> R<()> {
    let (w, h) = {
        let fb = c.fb.lock().unwrap();
        (fb.w as u16, fb.h as u16)
    };
    let mut b = [3, incremental as u8, 0, 0, 0, 0, 0, 0, 0, 0];
    b[6..8].copy_from_slice(&w.to_be_bytes());
    b[8..10].copy_from_slice(&h.to_be_bytes());
    send(s, &b).await
}

async fn send(s: &mut BufReader<UnixStream>, b: &[u8]) -> R<()> {
    s.get_mut().write_all(b).await.map_err(|e| rfb(e.to_string()))
}

async fn bytes(s: &mut BufReader<UnixStream>, n: usize) -> R<Vec<u8>> {
    let mut b = vec![0; n];
    s.read_exact(&mut b).await.map_err(|e| rfb(e.to_string()))?;
    Ok(b)
}

async fn reason(s: &mut BufReader<UnixStream>) -> R<String> {
    let n = u32::from_be_bytes(bytes(s, 4).await?.try_into().unwrap()) as usize;
    Ok(String::from_utf8_lossy(&bytes(s, n.min(1024)).await?).into_owned())
}

fn be16(b: &[u8]) -> usize {
    u16::from_be_bytes([b[0], b[1]]) as usize
}

fn encode(c: &Capture, max_width: usize) -> R<Arc<Vec<u8>>> {
    let mut cache = c.encoded.lock().unwrap();
    let (rgb, w, h, version) = {
        let fb = c.fb.lock().unwrap();
        if let Some((v, jpeg)) = cache.get(&max_width)
            && *v == fb.version
        {
            return Ok(jpeg.clone());
        }
        let w = max_width.min(fb.w);
        let h = ((fb.h as f64 * w as f64 / fb.w as f64).round() as usize).max(1);
        (scale(&fb.pixels, fb.w, fb.h, w, h), w, h, fb.version)
    };
    let mut jpeg = Vec::new();
    jpeg_encoder::Encoder::new(&mut jpeg, QUALITY)
        .encode(&rgb, w as u16, h as u16, jpeg_encoder::ColorType::Rgb)
        .map_err(|e| E::Failed(format!("jpeg: {e}")))?;
    let jpeg = Arc::new(jpeg);
    cache.insert(max_width, (version, jpeg.clone()));
    Ok(jpeg)
}

/// Box-filter downscale from B,G,R,X to R,G,B.
pub fn scale(bgrx: &[u8], sw: usize, sh: usize, w: usize, h: usize) -> Vec<u8> {
    let mut rgb = vec![0u8; w * h * 3];
    for oy in 0..h {
        let (y0, y1) = (oy * sh / h, ((oy + 1) * sh / h).max(oy * sh / h + 1));
        for ox in 0..w {
            let (x0, x1) = (ox * sw / w, ((ox + 1) * sw / w).max(ox * sw / w + 1));
            let (mut r, mut g, mut b) = (0usize, 0usize, 0usize);
            for y in y0..y1 {
                for px in bgrx[(y * sw + x0) * 4..(y * sw + x1) * 4].as_chunks::<4>().0 {
                    b += px[0] as usize;
                    g += px[1] as usize;
                    r += px[2] as usize;
                }
            }
            let n = (y1 - y0) * (x1 - x0);
            let o = (oy * w + ox) * 3;
            rgb[o..o + 3].copy_from_slice(&[(r / n) as u8, (g / n) as u8, (b / n) as u8]);
        }
    }
    rgb
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scales_bgrx_to_rgb() {
        // 2x1 B,G,R,X pixels: pure red and pure blue, averaged into one purple pixel.
        let px = [0, 0, 255, 0, 255, 0, 0, 0];
        assert_eq!(scale(&px, 2, 1, 1, 1), vec![127, 0, 127]);
        assert_eq!(scale(&px, 2, 1, 2, 1), vec![255, 0, 0, 0, 0, 255]);
    }

    #[tokio::test]
    async fn captures_a_frame_from_a_fake_server() {
        let (ours, mut server) = UnixStream::pair().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 64];
            server.write_all(b"RFB 003.008\n").await.unwrap();
            server.read_exact(&mut buf[..12]).await.unwrap();
            server.write_all(&[1, 1]).await.unwrap();
            server.read_exact(&mut buf[..1]).await.unwrap();
            server.write_all(&[0, 0, 0, 0]).await.unwrap();
            server.read_exact(&mut buf[..1]).await.unwrap();
            let mut init = vec![0, 2, 0, 1];
            init.extend([0; 16]);
            init.extend([0, 0, 0, 0]);
            server.write_all(&init).await.unwrap();
            server.read_exact(&mut buf[..20 + 12 + 10]).await.unwrap();
            // One raw 2x1 update: red, blue.
            let mut upd = vec![0, 0, 0, 1, 0, 0, 0, 0, 0, 2, 0, 1, 0, 0, 0, 0];
            upd.extend([0, 0, 255, 0, 255, 0, 0, 0]);
            server.write_all(&upd).await.unwrap();
            let mut sink = vec![0u8; 1024];
            while server.read(&mut sink).await.is_ok_and(|n| n > 0) {}
        });
        let caps = Captures::default();
        let mut ours = Some(ours);
        let jpeg = caps.frame("ludius-t", "1|x".into(), move || Box::pin(async move { Ok(ours.take().unwrap()) }), 640).await.unwrap();
        assert_eq!(&jpeg[..3], &[0xFF, 0xD8, 0xFF]);
        caps.close("ludius-t");
    }
}
