use crate::error::{E, R, down};
use ludius_proto::v1::{Hello, ObserverRequest, ObserverResponse, observer_response::Body};
use prost::Message;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;
use tokio_vsock::{VsockAddr, VsockStream};

/// The observer in one guest: length-prefixed (u32 LE) protobuf frames over vsock, Hello first.
pub struct Guest {
    name: String,
    cid: Option<u32>,
    port: u32,
    st: Mutex<State>,
}

#[derive(Default)]
struct State {
    s: Option<VsockStream>,
    next: u64,
    hello: Option<Hello>,
}

impl Guest {
    pub fn new(name: &str, cid: Option<u32>, port: u32) -> Self {
        Guest { name: name.into(), cid, port, st: Mutex::default() }
    }

    pub async fn request(&self, mut req: ObserverRequest, timeout: Duration) -> R<(ObserverResponse, Option<Hello>)> {
        let mut st = self.st.lock().await;
        for attempt in 0.. {
            match self.once(&mut st, &mut req, timeout).await {
                Ok(res) => return Ok((res, st.hello.clone())),
                Err(e) => {
                    st.s = None;
                    if attempt >= 1 {
                        return Err(E::Down(format!("observer unreachable: {e}")));
                    }
                }
            }
        }
        unreachable!()
    }

    async fn once(&self, st: &mut State, req: &mut ObserverRequest, timeout: Duration) -> R<ObserverResponse> {
        if st.s.is_none() {
            let (s, hello) = tokio::time::timeout(Duration::from_secs(5), self.connect())
                .await
                .map_err(|_| down("connect timed out"))??;
            st.s = Some(s);
            st.hello = Some(hello);
        }
        st.next += 1;
        req.id = st.next;
        let s = st.s.as_mut().unwrap();
        write(s, req).await?;
        let hello = &mut st.hello;
        tokio::time::timeout(timeout, async {
            loop {
                let res = ObserverResponse::decode(read(s).await?.as_slice()).map_err(down)?;
                if res.id == req.id {
                    return Ok(res);
                }
                if let Some(Body::Hello(h)) = res.body {
                    *hello = Some(h);
                }
            }
        })
        .await
        .map_err(|_| down("observer timed out"))?
    }

    async fn connect(&self) -> R<(VsockStream, Hello)> {
        let cid = self
            .cid
            .ok_or_else(|| E::Down(format!("{} has no vsock device (args: -device vhost-vsock-pci,guest-cid=N)", self.name)))?;
        let mut s = VsockStream::connect(VsockAddr::new(cid, self.port))
            .await
            .map_err(|e| E::Down(format!("vsock connect to cid {cid} port {}: {e}", self.port)))?;
        match ObserverResponse::decode(read(&mut s).await?.as_slice()).map_err(down)?.body {
            Some(Body::Hello(h)) => Ok((s, h)),
            _ => Err(down("observer did not say hello")),
        }
    }
}

async fn write(s: &mut VsockStream, m: &ObserverRequest) -> R<()> {
    let body = m.encode_to_vec();
    let mut frame = (body.len() as u32).to_le_bytes().to_vec();
    frame.extend(body);
    s.write_all(&frame).await.map_err(down)
}

async fn read(s: &mut VsockStream) -> R<Vec<u8>> {
    let mut len = [0u8; 4];
    s.read_exact(&mut len).await.map_err(down)?;
    let n = u32::from_le_bytes(len) as usize;
    if n > 16 << 20 {
        return Err(down("frame too large"));
    }
    let mut body = vec![0u8; n];
    s.read_exact(&mut body).await.map_err(down)?;
    Ok(body)
}
