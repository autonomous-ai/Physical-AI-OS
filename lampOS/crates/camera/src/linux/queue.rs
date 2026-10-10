//! Private MMAP wrapper. Neither FD nor mapping nor a mapped reference escapes.
//! Driver DMA owns a slot while queued; Rust borrows its bytes only after a
//! checked successful DQBUF and ends that borrow before QBUF. No USERPTR/DMABUF.
use super::{identity::Opened, map_ioctl};
use crate::PortError;
use crate::backend::{Dequeued, Driver, Layout, SLOTS};
use std::os::fd::OwnedFd;
use v4l2r::{QueueType, ioctl, memory::MemoryType};

const QUEUE: QueueType = QueueType::VideoCapture;
#[derive(Clone, Copy, Eq, PartialEq)]
enum State {
    Empty,
    Mapped,
    Queued,
    Dequeued,
    Unknown,
}

pub(super) struct LinuxDriver {
    pub opened: Opened,
    mappings: [Option<ioctl::PlaneMapping>; SLOTS],
    states: [State; SLOTS],
    lengths: [usize; SLOTS],
}
impl LinuxDriver {
    pub(super) fn new(opened: Opened) -> Self {
        Self {
            opened,
            mappings: std::array::from_fn(|_| None),
            states: [State::Empty; SLOTS],
            lengths: [0; SLOTS],
        }
    }
    fn fd(&self) -> Result<&OwnedFd, PortError> {
        self.opened.fd.as_ref().ok_or(PortError::InvalidData)
    }
    fn validate_raw(raw: &ioctl::UncheckedV4l2Buffer) -> Result<(), PortError> {
        if raw.0.type_ != QUEUE as u32 || raw.0.memory != MemoryType::Mmap as u32 || raw.1.is_some()
        {
            return Err(PortError::InvalidData);
        }
        Ok(())
    }
}
impl Driver for LinuxDriver {
    fn request_buffers(&mut self, count: u32) -> Result<u32, PortError> {
        let reply: ioctl::RequestBuffers = ioctl::reqbufs(
            self.fd()?,
            QUEUE,
            MemoryType::Mmap,
            count,
            ioctl::MemoryConsistency::empty(),
        )
        .map_err(map_ioctl)?;
        // Zero capability bits are allowed on older kernels. Nonzero capability
        // flags must include support for the memory mode we actually requested.
        if !reply.capabilities.is_empty()
            && !reply
                .capabilities
                .contains(ioctl::BufferCapabilities::SUPPORTS_MMAP)
        {
            return Err(PortError::Unsupported);
        }
        Ok(reply.count)
    }
    fn query_buffer(&mut self, index: u32) -> Result<Layout, PortError> {
        let raw: ioctl::UncheckedV4l2Buffer =
            ioctl::querybuf(self.fd()?, QUEUE, index as usize).map_err(map_ioctl)?;
        Self::validate_raw(&raw)?;
        let reply = ioctl::QueryBuffer::from(raw);
        if reply.index != index as usize || reply.planes.len() != 1 {
            return Err(PortError::InvalidData);
        }
        Ok(Layout {
            index,
            length: reply.planes[0].length as usize,
            offset: reply.planes[0].mem_offset,
        })
    }
    fn map(&mut self, layout: Layout) -> Result<(), PortError> {
        let index = layout.index as usize;
        if index >= SLOTS
            || self.states[index] != State::Empty
            || layout.length == 0
            || layout.length > crate::MAX_FRAME_BYTES
        {
            return Err(PortError::InvalidData);
        }
        let mapping =
            ioctl::mmap(self.fd()?, layout.offset, layout.length as u32).map_err(map_ioctl)?;
        if mapping.size() != layout.length {
            return Err(PortError::InvalidData);
        }
        self.mappings[index] = Some(mapping);
        self.lengths[index] = layout.length;
        self.states[index] = State::Mapped;
        Ok(())
    }
    fn queue(&mut self, index: u32) -> Result<(), PortError> {
        let slot = index as usize;
        if slot >= SLOTS || !matches!(self.states[slot], State::Mapped | State::Dequeued) {
            return Err(PortError::InvalidData);
        }
        self.states[slot] = State::Unknown;
        let request = ioctl::V4l2Buffer::new(QUEUE, index, MemoryType::Mmap);
        let raw: ioctl::UncheckedV4l2Buffer =
            ioctl::qbuf(self.fd()?, request).map_err(map_ioctl)?;
        Self::validate_raw(&raw)?;
        if raw.0.index != index {
            return Err(PortError::InvalidData);
        }
        self.states[slot] = State::Queued;
        Ok(())
    }
    fn stream_on(&mut self) -> Result<(), PortError> {
        ioctl::streamon(self.fd()?, QUEUE).map_err(map_ioctl)
    }
    fn dequeue(&mut self) -> Result<Option<Dequeued>, PortError> {
        let raw: ioctl::UncheckedV4l2Buffer = match ioctl::dqbuf(self.fd()?, QUEUE) {
            Ok(raw) => raw,
            Err(error) => {
                let error = map_ioctl(error);
                if error == PortError::Io(11) {
                    return Ok(None);
                }
                return Err(error);
            }
        };
        Self::validate_raw(&raw)?;
        let buffer = raw.0;
        let index = buffer.index as usize;
        if index >= SLOTS || self.states[index] != State::Queued {
            return Err(PortError::InvalidData);
        }
        self.states[index] = State::Dequeued;
        Ok(Some(Dequeued {
            index: buffer.index,
            length: buffer.length as usize,
            bytes_used: buffer.bytesused as usize,
            sequence: buffer.sequence,
            flags: buffer.flags,
            seconds: buffer.timestamp.tv_sec,
            micros: buffer.timestamp.tv_usec,
        }))
    }
    fn copy_dequeued(&mut self, index: u32, destination: &mut [u8]) -> Result<(), PortError> {
        let index = index as usize;
        if index >= SLOTS
            || self.states[index] != State::Dequeued
            || destination.len() > self.lengths[index]
        {
            return Err(PortError::InvalidData);
        }
        let mapping = self.mappings[index]
            .as_ref()
            .ok_or(PortError::InvalidData)?;
        // This borrow ends at return, before the generic queue may requeue.
        destination.copy_from_slice(&mapping.as_ref()[..destination.len()]);
        Ok(())
    }
    fn stream_off(&mut self) -> Result<(), PortError> {
        let result = ioctl::streamoff(self.fd()?, QUEUE).map_err(map_ioctl);
        self.states.fill(State::Unknown);
        result
    }
    fn unmap_all(&mut self) {
        // Upstream PlaneMapping::drop logs munmap errors but cannot return them.
        // No data is read during cleanup; process exit is the final VM boundary.
        self.mappings = std::array::from_fn(|_| None);
        self.states.fill(State::Empty);
        self.lengths.fill(0);
    }
    fn release_buffers(&mut self) -> Result<(), PortError> {
        let reply: ioctl::RequestBuffers = ioctl::reqbufs(
            self.fd()?,
            QUEUE,
            MemoryType::Mmap,
            0,
            ioctl::MemoryConsistency::empty(),
        )
        .map_err(map_ioctl)?;
        if reply.count != 0 {
            return Err(PortError::InvalidData);
        }
        Ok(())
    }
    fn close(&mut self) -> Result<(), PortError> {
        self.opened.close()
    }
}
impl Drop for LinuxDriver {
    fn drop(&mut self) {
        self.unmap_all();
        let _ = self.opened.close();
    }
}
