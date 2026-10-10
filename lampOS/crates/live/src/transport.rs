//! Two independent process channels. Control can progress while PCM is full.
use crate::wire::{Envelope, ReceiveOrder, ReferenceControl, decode, encode};
use lamp_interaction::BootId;
use lamp_ipc::{Endpoint, MAX_DATAGRAM_BYTES, monotonic_us};
use serde::{Serialize, de::DeserializeOwned};
use std::{io, path::Path};

pub struct Channel {
    endpoint: Endpoint,
    sender: BootId,
    send_sequence: u64,
    incoming: ReceiveOrder,
    max_age_us: u64,
}

impl Channel {
    pub fn bind(path: &Path, sender: BootId, peer: BootId, max_age_us: u64) -> io::Result<Self> {
        if max_age_us == 0 || max_age_us > 1_000_000 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid channel freshness bound",
            ));
        }
        Ok(Self {
            endpoint: Endpoint::bind(path)?,
            sender,
            send_sequence: 0,
            incoming: ReceiveOrder::new(peer),
            max_age_us,
        })
    }
    pub fn connect(&mut self, path: &Path) -> io::Result<()> {
        self.endpoint.connect(path)
    }

    pub fn send<T: Serialize>(&mut self, payload: T) -> io::Result<()> {
        let sequence = self
            .send_sequence
            .checked_add(1)
            .ok_or_else(|| io::Error::other("channel sequence exhausted"))?;
        let data = encode(&Envelope {
            boot: self.sender,
            sequence,
            sent_at_us: monotonic_us(),
            payload,
        })?;
        self.endpoint.try_send(&data)?;
        self.send_sequence = sequence;
        Ok(())
    }

    pub fn receive<T: DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        let mut data = [0; MAX_DATAGRAM_BYTES];
        let count = match self.endpoint.try_recv(&mut data) {
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(None),
            Err(error) => return Err(error),
        };
        let packet: Envelope<T> = decode(&data[..count])?;
        self.incoming
            .accept(&packet, monotonic_us(), self.max_age_us)?;
        Ok(Some(packet.payload))
    }
}

pub struct WorkerChannels {
    pub control: Channel,
    pub data: Channel,
}

impl WorkerChannels {
    pub fn bind(
        dir: &Path,
        role: &str,
        parent: BootId,
        worker: BootId,
        worker_side: bool,
    ) -> io::Result<Self> {
        if !matches!(
            role,
            "capture" | "speaker" | "privacy" | "provider" | "ring" | "probe" | "camera"
        ) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid worker role",
            ));
        }
        let side = if worker_side { "w" } else { "p" };
        let (sender, peer) = if worker_side {
            (worker, parent)
        } else {
            (parent, worker)
        };
        Ok(Self {
            control: Channel::bind(&dir.join(format!("{role}.c.{side}")), sender, peer, 100_000)?,
            data: Channel::bind(&dir.join(format!("{role}.d.{side}")), sender, peer, 100_000)?,
        })
    }
    /// The peer must already have bound both sockets; startup retries are bounded
    /// by the supervisor and must not attempt to reconnect an established side.
    pub fn connect(&mut self, dir: &Path, role: &str, worker_side: bool) -> io::Result<()> {
        let side = if worker_side { "p" } else { "w" };
        self.control
            .connect(&dir.join(format!("{role}.c.{side}")))?;
        self.data.connect(&dir.join(format!("{role}.d.{side}")))
    }
}

/// Direct private reference channels. The peer boot is supplied only over the
/// trusted controller connection; it is never learned from a render packet.
/// Connecting is a bounded startup state, never a blocking audio-loop wait.
pub struct ReferenceChannels {
    pub control: Channel,
    pub data: Channel,
    peer_control: std::path::PathBuf,
    peer_data: std::path::PathBuf,
    control_connected: bool,
    connected: bool,
    deadline_us: u64,
}

impl ReferenceChannels {
    pub fn bind(dir: &Path, role: &str, own: BootId, peer: BootId) -> io::Result<Self> {
        let other = match role {
            "speaker" => "capture",
            "capture" => "speaker",
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "invalid reference role",
                ));
            }
        };
        Ok(Self {
            control: Channel::bind(&dir.join(format!("ref.c.{role}")), own, peer, 100_000)?,
            data: Channel::bind(&dir.join(format!("ref.d.{role}")), own, peer, 100_000)?,
            peer_control: dir.join(format!("ref.c.{other}")),
            peer_data: dir.join(format!("ref.d.{other}")),
            control_connected: false,
            connected: false,
            deadline_us: monotonic_us()
                .checked_add(3_000_000)
                .ok_or_else(|| io::Error::other("reference deadline overflow"))?,
        })
    }

    /// Optional terminal notice, only after successful local PCM stop and reset.
    /// The independently stopped capture peer may already have closed its socket.
    /// Make at most one nonblocking send on an established control connection;
    /// never connect, retry, wait, or infer a peer acknowledgement here. Delivery
    /// failure cannot turn the completed local shutdown into an audio fault.
    /// Running reference/control/data traffic must still use the strict channels.
    pub fn notify_stopped_after_local_close(&mut self, playback_epoch: u64) {
        if self.control_connected {
            let _ = self.control.send(ReferenceControl::Stopped {
                playback_epoch,
                observed_at_us: monotonic_us(),
            });
        }
    }

    pub fn connect_step(&mut self) -> io::Result<bool> {
        if self.connected {
            return Ok(true);
        }
        if monotonic_us() >= self.deadline_us {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "direct reference connection timed out",
            ));
        }
        if !self.control_connected {
            match self.control.connect(&self.peer_control) {
                Ok(()) => self.control_connected = true,
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
                Err(error) => return Err(error),
            }
        }
        match self.data.connect(&self.peer_data) {
            Ok(()) => {
                self.connected = true;
                Ok(true)
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }
}
