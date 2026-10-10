//! In-memory scripted service for deterministic tests of this transport and
//! of the callers built on it. There is no TLS, socket, credential or cloud
//! request here, and nothing in this module is used by the runtime.
use crate::{
    Attempt, Connect, Connection, Result, ResumptionHandle, SessionConfig, SessionId,
    session::{setup_and_spawn, socket_config},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::time::Duration;
use tokio::{io::DuplexStream, sync::mpsc, time::timeout};
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{
        Message,
        protocol::{CloseFrame, Role},
    },
};

const RECEIVE_TIMEOUT: Duration = Duration::from_secs(2);

/// The server end of one scripted connection.
pub struct FakeService {
    socket: WebSocketStream<DuplexStream>,
}
impl FakeService {
    /// Next client JSON message. Panics when none arrives within two seconds.
    pub async fn receive(&mut self) -> Value {
        self.try_receive(RECEIVE_TIMEOUT)
            .await
            .expect("client message")
    }
    /// Next client JSON message, or `None` on silence, close or stream end.
    pub async fn try_receive(&mut self, wait: Duration) -> Option<Value> {
        let deadline = tokio::time::Instant::now() + wait;
        loop {
            match tokio::time::timeout_at(deadline, self.socket.next()).await {
                Ok(Some(Ok(Message::Text(text)))) => {
                    return serde_json::from_slice(text.as_bytes()).ok();
                }
                Ok(Some(Ok(Message::Binary(bytes)))) => return serde_json::from_slice(&bytes).ok(),
                Ok(Some(Ok(Message::Ping(_) | Message::Pong(_)))) => {}
                _ => return None,
            }
        }
    }
    /// Receive the setup message and acknowledge it.
    pub async fn accept(&mut self) -> Value {
        let setup = self.receive().await;
        self.send(json!({"setupComplete": {}})).await;
        setup
    }
    pub async fn send(&mut self, value: Value) {
        self.socket
            .send(Message::text(value.to_string()))
            .await
            .expect("scripted server send");
    }
    /// Write every message before the client can read any of them: the shape
    /// of a burst released by the network after a stall.
    pub async fn burst(&mut self, values: impl IntoIterator<Item = Value>) {
        for value in values {
            self.socket
                .feed(Message::text(value.to_string()))
                .await
                .expect("scripted server feed");
        }
        self.socket.flush().await.expect("scripted server flush");
    }
    pub async fn close(&mut self, code: u16, reason: &str) {
        let _ = self
            .socket
            .close(Some(CloseFrame {
                code: code.into(),
                reason: reason.to_owned().into(),
            }))
            .await;
    }
}

/// One server message carrying 24 kHz PCM, every sample equal to `value`.
pub fn audio(value: i16, samples: usize) -> Value {
    let bytes: Vec<u8> = std::iter::repeat_n(value.to_le_bytes(), samples)
        .flatten()
        .collect();
    json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{
        "mimeType":"audio/pcm;rate=24000","data":STANDARD.encode(bytes)}}]}}})
}

/// A client connection attempt and the scripted service at its other end. The
/// attempt completes only after the service acknowledges setup.
pub async fn pair(
    config: SessionConfig,
    session: SessionId,
    capacity: usize,
) -> (
    impl Future<Output = Result<Connection>> + Send + 'static,
    FakeService,
) {
    let (client, server) = tokio::io::duplex(capacity);
    let client =
        WebSocketStream::from_raw_socket(client, Role::Client, Some(socket_config())).await;
    let server =
        WebSocketStream::from_raw_socket(server, Role::Server, Some(socket_config())).await;
    (
        setup_and_spawn(client, config, session),
        FakeService { socket: server },
    )
}

/// A dial observed by a test: which session, whether a resumption handle was
/// presented, and the service that will answer it.
pub struct Dial {
    pub session: SessionId,
    pub resumed: bool,
    pub service: FakeService,
}

/// Connector whose every attempt is handed to the test as a [`Dial`]. Dropping
/// the service without accepting fails that attempt.
pub struct FakeConnector {
    config: SessionConfig,
    dials: mpsc::UnboundedSender<Dial>,
}
pub fn connector(config: SessionConfig) -> (FakeConnector, mpsc::UnboundedReceiver<Dial>) {
    let (dials, observed) = mpsc::unbounded_channel();
    (FakeConnector { config, dials }, observed)
}
impl Connect for FakeConnector {
    fn connect(&mut self, session: SessionId, resume: Option<ResumptionHandle>) -> Attempt {
        let resumed = resume.is_some();
        let config = self.config.clone().resume(resume);
        let dials = self.dials.clone();
        Box::pin(async move {
            let (attempt, service) = pair(config, session, crate::MAX_WIRE_BYTES * 2).await;
            let _ = dials.send(Dial {
                session,
                resumed,
                service,
            });
            attempt.await
        })
    }
}

/// Next dial, or a panic when the supervisor does not attempt one in time.
pub async fn next_dial(observed: &mut mpsc::UnboundedReceiver<Dial>) -> Dial {
    timeout(RECEIVE_TIMEOUT, observed.recv())
        .await
        .expect("connection attempt")
        .expect("connector alive")
}
