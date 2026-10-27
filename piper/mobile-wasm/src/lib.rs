//! Browser-only QUIC transport: iroh verifies the PC key before any pairing secret is sent.
use futures_util::lock::Mutex;
use futures_util::future::{AbortHandle, Abortable};
use std::cell::{Cell, RefCell};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMap, RelayMode, RelayUrl, SecretKey};
use iroh::endpoint::{Connection, RecvStream, SendStream, presets};
use wasm_bindgen::prelude::*;

fn error(e: impl std::fmt::Display) -> JsError { JsError::new(&e.to_string()) }

#[wasm_bindgen]
pub struct MobileConnection { endpoint: Endpoint, connection: Connection }

#[wasm_bindgen]
impl MobileConnection {
    /// Explicit relay and pinned server; no DNS discovery or ambient public relay.
    pub async fn connect(server: String, relay: String, secret: Vec<u8>) -> Result<Self, JsError> {
        let key: [u8; 32] = secret.try_into().map_err(|_| error("invalid device key"))?;
        let server: EndpointId = server.parse().map_err(error)?;
        let relay: RelayUrl = relay.parse().map_err(|_| error("Invalid relay URL"))?;
        if relay.scheme() != "https" { return Err(error("HTTPS relay required")); }
        let endpoint = Endpoint::builder(presets::Minimal)
            .secret_key(SecretKey::from_bytes(&key))
            .relay_mode(RelayMode::Custom(RelayMap::from_iter([relay.clone()])))
            .bind().await.map_err(|_| error("Mobile endpoint could not start"))?;
        let target = EndpointAddr::new(server).with_relay_url(relay);
        match endpoint.connect(target, b"piper/mobile/1").await {
            Ok(connection) => Ok(Self { endpoint, connection }),
            Err(_) => { endpoint.close().await; Err(error("Relay connection failed")) }
        }
    }
    pub async fn open(&self) -> Result<MobileStream, JsError> {
        let (send, recv) = self.connection.open_bi().await.map_err(error)?;
        Ok(MobileStream { send: Mutex::new(send), recv: Mutex::new(recv), read_abort: RefCell::new(None), cancelled: Cell::new(false) })
    }
    pub async fn close(&self) { self.connection.close(0u8.into(), b"client closed"); self.endpoint.close().await; }
}

#[wasm_bindgen]
pub struct MobileStream { send: Mutex<SendStream>, recv: Mutex<RecvStream>, read_abort: RefCell<Option<AbortHandle>>, cancelled: Cell<bool> }

#[wasm_bindgen]
impl MobileStream {
    pub async fn write(&self, data: Vec<u8>) -> Result<(), JsError> {
        if self.cancelled.get() { return Err(error("stream cancelled")); }
        if data.len() > (16 << 20) + 8196 { return Err(error("request too large")); }
        self.send.lock().await.write_all(&data).await.map_err(error)
    }
    pub async fn finish(&self) -> Result<(), JsError> { self.send.lock().await.finish().map_err(error) }
    pub async fn read(&self, count: u32) -> Result<Vec<u8>, JsError> {
        if count > (32 << 20) { return Err(error("response too large")); }
        let mut bytes = vec![0; count as usize];
        let mut recv = self.recv.lock().await;
        if self.cancelled.get() { return Err(error("stream cancelled")); }
        let (abort, registration) = AbortHandle::new_pair();
        *self.read_abort.borrow_mut() = Some(abort);
        let result = Abortable::new(recv.read_exact(&mut bytes), registration).await;
        self.read_abort.borrow_mut().take();
        result.map_err(error)?.map_err(error)?;
        Ok(bytes)
    }
    pub async fn read_to_end(&self) -> Result<Vec<u8>, JsError> {
        let mut recv = self.recv.lock().await;
        if self.cancelled.get() { return Err(error("stream cancelled")); }
        let (abort, registration) = AbortHandle::new_pair();
        *self.read_abort.borrow_mut() = Some(abort);
        let result = Abortable::new(recv.read_to_end((32 << 20) + 8196), registration).await;
        self.read_abort.borrow_mut().take();
        result.map_err(error)?.map_err(error)
    }
    pub async fn cancel(&self) {
        self.cancelled.set(true);
        if let Some(abort) = self.read_abort.borrow_mut().take() { abort.abort(); }
        let _ = self.send.lock().await.reset(0u8.into());
        let _ = self.recv.lock().await.stop(0u8.into());
    }
}
