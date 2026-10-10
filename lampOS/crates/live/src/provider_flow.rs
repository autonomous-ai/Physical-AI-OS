//! Capacity is not output authority. These boot-scoped counters reserve at most
//! two worst-case PCM packets; ownership and the 100 ms IPC checks still apply.
use crate::{transport::Channel, wire::Control};
use std::io;

pub(crate) const PACKET_SAMPLES: usize = 960;
pub(crate) const MAX_REPLY_SAMPLES: usize = 24_000 * 30;
const PACKET_WINDOW: u64 = 2;

#[cfg(test)]
mod tests;

pub(crate) struct OutputSender {
    sent: u64,
    through: u64,
}
impl Default for OutputSender {
    fn default() -> Self {
        Self {
            sent: 0,
            through: PACKET_WINDOW,
        }
    }
}
impl OutputSender {
    pub(crate) fn grant(&mut self, through: u64) -> io::Result<()> {
        if through < self.through
            || through
                > self
                    .sent
                    .checked_add(PACKET_WINDOW)
                    .ok_or_else(|| io::Error::other("provider packet counter exhausted"))?
        {
            return Err(io::Error::other("invalid provider output capacity"));
        }
        self.through = through;
        Ok(())
    }

    pub(crate) fn try_send(&mut self, send: impl FnOnce() -> io::Result<()>) -> io::Result<bool> {
        if !self.can_send() {
            return Ok(false);
        }
        match send() {
            Ok(()) => {
                self.record_sent()?;
                Ok(true)
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(false),
            Err(error) => Err(error),
        }
    }

    pub(crate) fn can_send(&self) -> bool {
        self.sent < self.through
    }

    /// Called only after a successful send, with capacity checked before I/O.
    pub(crate) fn record_sent(&mut self) -> io::Result<()> {
        if !self.can_send() {
            return Err(io::Error::other("provider sent without capacity"));
        }
        self.sent += 1;
        Ok(())
    }
}

pub(crate) struct OutputReceiver {
    received: u64,
    granted: u64,
}
impl Default for OutputReceiver {
    fn default() -> Self {
        Self {
            received: 0,
            granted: PACKET_WINDOW,
        }
    }
}
impl OutputReceiver {
    /// Count every successfully decoded output, including discarded old-owner
    /// audio, setup, transcripts and lifecycle events. Never reset for a turn.
    pub(crate) fn received(&mut self) -> io::Result<()> {
        if self.received >= self.granted {
            return Err(io::Error::other("provider exceeded output capacity"));
        }
        self.received += 1;
        Ok(())
    }

    pub(crate) fn replenish(
        &mut self,
        buffered_samples: usize,
        control: &mut Channel,
    ) -> io::Result<()> {
        self.replenish_with(buffered_samples, |through| {
            control.send(Control::ProviderOutputCapacity { through })
        })
    }

    fn replenish_with(
        &mut self,
        buffered_samples: usize,
        send: impl FnOnce(u64) -> io::Result<()>,
    ) -> io::Result<()> {
        let free = MAX_REPLY_SAMPLES
            .checked_sub(buffered_samples)
            .ok_or_else(|| io::Error::other("reply exceeded reserved audio capacity"))?;
        let available = ((free / PACKET_SAMPLES) as u64).min(PACKET_WINDOW);
        let through = self
            .received
            .checked_add(available)
            .ok_or_else(|| io::Error::other("provider packet counter exhausted"))?;
        if through < self.granted {
            return Err(io::Error::other("provider output reservation regressed"));
        }
        if through == self.granted {
            return Ok(());
        }
        // Coalesce an unsent grant on the next tick. It cannot bypass priority
        // authority, consume memory, or invent a receipt from the producer.
        match send(through) {
            Ok(()) => {
                self.granted = through;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(error) => Err(error),
        }
    }
}
