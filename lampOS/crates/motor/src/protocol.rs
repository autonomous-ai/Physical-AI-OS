//! Bounded, read-only request encoding and incremental status framing.
//!
//! Status packets carry no transaction number or register address. Matching an
//! ID and length cannot establish freshness. One future bus owner must drain
//! prior traffic and enforce monotonic transaction deadlines before using this
//! codec. Never pipeline ambiguous requests on the shared bus.

use crate::{JOINT_COUNT, Joint};
use std::fmt;

/// Strict total-frame bound. The legacy SDK's TX bound is 250; its RX length
/// check accidentally allows four additional bytes. This implementation does not.
pub const MAX_PACKET_BYTES: usize = 250;
pub const MAX_STATUS_PARAMETERS: usize = MAX_PACKET_BYTES - 6;
pub const MAX_REQUEST_BYTES: usize = JOINT_COUNT + 8;
const BROADCAST_ID: u8 = 0xfe;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReadWindow {
    address: u8,
    byte_count: u8,
}

impl ReadWindow {
    pub const MODEL_NUMBER: Self = Self {
        address: 3,
        byte_count: 2,
    };
    pub const POSITION_LIMITS: Self = Self {
        address: 9,
        byte_count: 4,
    };
    pub const HOMING_AND_MODE: Self = Self {
        address: 31,
        byte_count: 3,
    };
    pub const TELEMETRY: Self = Self {
        address: 56,
        byte_count: 15,
    };

    /// All requests are reads. A window must fit one bounded reply and must not
    /// wrap the 8-bit register address space.
    pub fn new(address: u8, byte_count: u8) -> Result<Self, RequestError> {
        if byte_count == 0 || usize::from(byte_count) > MAX_STATUS_PARAMETERS {
            return Err(RequestError::InvalidReadCount(byte_count));
        }
        if u16::from(address) + u16::from(byte_count) > 256 {
            return Err(RequestError::ReadAddressOverflow {
                address,
                byte_count,
            });
        }
        Ok(Self {
            address,
            byte_count,
        })
    }

    pub const fn address(self) -> u8 {
        self.address
    }

    pub const fn byte_count(self) -> u8 {
        self.byte_count
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplyKind {
    Ping,
    Read(ReadWindow),
}

impl ReplyKind {
    const fn parameter_count(self) -> usize {
        match self {
            Self::Ping => 0,
            Self::Read(window) => window.byte_count as usize,
        }
    }
}

/// Only ping, read, and sync-read constructors exist. Byte storage is immutable
/// through the public API; there is no arbitrary instruction or register writer.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReadOnlyRequest {
    bytes: [u8; MAX_REQUEST_BYTES],
    length: u8,
    expected_mask: u8,
    kind: ReplyKind,
}

impl ReadOnlyRequest {
    pub fn ping(joint: Joint) -> Self {
        let mut request = Self {
            bytes: [0; MAX_REQUEST_BYTES],
            length: 6,
            expected_mask: joint.mask(),
            kind: ReplyKind::Ping,
        };
        request.bytes[..5].copy_from_slice(&[0xff, 0xff, joint.id(), 2, 1]);
        request.set_checksum();
        request
    }

    pub fn read(joint: Joint, window: ReadWindow) -> Self {
        let mut request = Self {
            bytes: [0; MAX_REQUEST_BYTES],
            length: 8,
            expected_mask: joint.mask(),
            kind: ReplyKind::Read(window),
        };
        request.bytes[..7].copy_from_slice(&[
            0xff,
            0xff,
            joint.id(),
            4,
            2,
            window.address,
            window.byte_count,
        ]);
        request.set_checksum();
        request
    }

    pub fn sync_read(joints: &[Joint], window: ReadWindow) -> Result<Self, RequestError> {
        if joints.is_empty() || joints.len() > JOINT_COUNT {
            return Err(RequestError::InvalidJointCount(joints.len()));
        }
        let mut expected_mask = 0;
        for &joint in joints {
            if expected_mask & joint.mask() != 0 {
                return Err(RequestError::DuplicateJoint(joint));
            }
            expected_mask |= joint.mask();
        }
        let mut request = Self {
            bytes: [0; MAX_REQUEST_BYTES],
            length: (joints.len() + 8) as u8,
            expected_mask,
            kind: ReplyKind::Read(window),
        };
        request.bytes[..7].copy_from_slice(&[
            0xff,
            0xff,
            BROADCAST_ID,
            (joints.len() + 4) as u8,
            0x82,
            window.address,
            window.byte_count,
        ]);
        for (index, joint) in joints.iter().enumerate() {
            request.bytes[index + 7] = joint.id();
        }
        request.set_checksum();
        Ok(request)
    }

    fn set_checksum(&mut self) {
        let last = usize::from(self.length) - 1;
        self.bytes[last] = checksum(&self.bytes[2..last]);
    }

    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..usize::from(self.length)]
    }

    pub const fn reply_kind(&self) -> ReplyKind {
        self.kind
    }

    pub fn track_replies(&self) -> ReplyTracker {
        ReplyTracker {
            expected_mask: self.expected_mask,
            received_mask: 0,
            kind: self.kind,
            failure: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestError {
    InvalidReadCount(u8),
    ReadAddressOverflow { address: u8, byte_count: u8 },
    InvalidJointCount(usize),
    DuplicateJoint(Joint),
}

impl fmt::Display for RequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidReadCount(count) => write!(f, "read count {count} is outside 1..=244"),
            Self::ReadAddressOverflow {
                address,
                byte_count,
            } => {
                write!(
                    f,
                    "read window {address}+{byte_count} wraps the register address"
                )
            }
            Self::InvalidJointCount(count) => {
                write!(f, "sync-read joint count {count} is outside 1..=5")
            }
            Self::DuplicateJoint(joint) => write!(f, "sync-read repeats {joint}"),
        }
    }
}

impl std::error::Error for RequestError {}

/// Preserve every error bit; unknown low bits are faults too. The SDK identifies
/// voltage=01, angle=02, overheat=04, electrical=08, overload=20.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StatusFlags(u8);

impl StatusFlags {
    pub const VOLTAGE: u8 = 0x01;
    pub const ANGLE: u8 = 0x02;
    pub const OVERHEAT: u8 = 0x04;
    pub const ELECTRICAL: u8 = 0x08;
    pub const OVERLOAD: u8 = 0x20;
    pub const KNOWN_MASK: u8 = 0x2f;

    pub const fn bits(self) -> u8 {
        self.0
    }

    pub const fn unknown_bits(self) -> u8 {
        self.0 & !Self::KNOWN_MASK
    }

    pub const fn is_clear(self) -> bool {
        self.0 == 0
    }
}

/// Checksum-valid framing is not a successful transaction. Use ReplyTracker to
/// validate the requested joint, error flags, payload length and duplicate IDs.
#[derive(Clone, Eq, PartialEq)]
pub struct StatusPacket {
    raw_id: u8,
    flags: StatusFlags,
    data: [u8; MAX_STATUS_PARAMETERS],
    data_length: u8,
}

impl StatusPacket {
    pub const fn raw_id(&self) -> u8 {
        self.raw_id
    }

    pub const fn flags(&self) -> StatusFlags {
        self.flags
    }

    pub fn parameters(&self) -> &[u8] {
        &self.data[..usize::from(self.data_length)]
    }
}

impl fmt::Debug for StatusPacket {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StatusPacket")
            .field("raw_id", &self.raw_id)
            .field("flags", &self.flags)
            .field("parameters", &self.parameters())
            .finish()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParseError {
    InvalidId(u8),
    InvalidLength(u8),
    InvalidStatusByte(u8),
    Checksum {
        expected: u8,
        received: u8,
    },
    Truncated {
        buffered: usize,
        expected: Option<usize>,
    },
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidId(id) => write!(f, "invalid status ID {id:#04x}"),
            Self::InvalidLength(length) => write!(f, "status length {length} is outside 2..=246"),
            Self::InvalidStatusByte(flags) => {
                write!(f, "status byte {flags:#04x} has reserved bit 7")
            }
            Self::Checksum { expected, received } => write!(
                f,
                "status checksum {received:#04x}, expected {expected:#04x}"
            ),
            Self::Truncated { buffered, expected } => write!(
                f,
                "incomplete status: {buffered} bytes, expected {expected:?}"
            ),
        }
    }
}

impl std::error::Error for ParseError {}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FeedReport {
    pub packets: usize,
    pub malformed: usize,
    /// Noise and bytes discarded while resynchronizing, excluding valid frames.
    pub discarded_bytes: usize,
}

/// Fixed 250-byte retained storage, no queue or allocation. Events are delivered
/// as they are parsed, including malformed frames. Feed bounded input chunks in
/// the owning process; caller callbacks and input length determine call duration.
#[derive(Clone, Debug)]
pub struct StatusParser {
    buffer: [u8; MAX_PACKET_BYTES],
    used: usize,
}

impl Default for StatusParser {
    fn default() -> Self {
        Self {
            buffer: [0; MAX_PACKET_BYTES],
            used: 0,
        }
    }
}

impl StatusParser {
    pub fn buffered_bytes(&self) -> usize {
        self.used
    }

    pub fn reset(&mut self) -> usize {
        let discarded = self.used;
        self.buffer.fill(0);
        self.used = 0;
        discarded
    }

    /// Call on deadline/end-of-input. Truncation is explicit and retained bytes
    /// are discarded even on failure, never replayed into a later transaction.
    pub fn finish(&mut self) -> Result<(), ParseError> {
        let result = if self.used == 0 {
            Ok(())
        } else {
            Err(ParseError::Truncated {
                buffered: self.used,
                expected: (self.used >= 4).then(|| usize::from(self.buffer[3]) + 4),
            })
        };
        self.reset();
        result
    }

    pub fn feed(
        &mut self,
        input: &[u8],
        mut emit: impl FnMut(Result<StatusPacket, ParseError>),
    ) -> FeedReport {
        let mut report = FeedReport::default();
        for &byte in input {
            // drain() consumes or rejects a candidate when its total length is
            // available. It can retain at most MAX_PACKET_BYTES - 1 bytes.
            self.buffer[self.used] = byte;
            self.used += 1;
            self.drain(&mut emit, &mut report);
        }
        report
    }

    fn remove_prefix(&mut self, count: usize) {
        self.buffer.copy_within(count..self.used, 0);
        self.used -= count;
    }

    fn drain(
        &mut self,
        emit: &mut impl FnMut(Result<StatusPacket, ParseError>),
        report: &mut FeedReport,
    ) {
        loop {
            if self.used == 0 {
                return;
            }
            if self.buffer[0] != 0xff || (self.used >= 2 && self.buffer[1] != 0xff) {
                self.remove_prefix(1);
                report.discarded_bytes += 1;
                continue;
            }
            if self.used < 4 {
                return;
            }
            let id = self.buffer[2];
            let length = self.buffer[3];
            let error = if id >= BROADCAST_ID {
                Some(ParseError::InvalidId(id))
            } else if !(2..=(MAX_PACKET_BYTES as u8 - 4)).contains(&length) {
                Some(ParseError::InvalidLength(length))
            } else if self.used >= 5 && self.buffer[4] & 0x80 != 0 {
                Some(ParseError::InvalidStatusByte(self.buffer[4]))
            } else {
                None
            };
            if let Some(error) = error {
                self.remove_prefix(1);
                report.discarded_bytes += 1;
                report.malformed += 1;
                emit(Err(error));
                continue;
            }
            let total = usize::from(length) + 4;
            if self.used < total {
                return;
            }
            let expected = checksum(&self.buffer[2..total - 1]);
            let received = self.buffer[total - 1];
            if expected != received {
                self.remove_prefix(1);
                report.discarded_bytes += 1;
                report.malformed += 1;
                emit(Err(ParseError::Checksum { expected, received }));
                continue;
            }
            let data_length = length - 2;
            let mut packet = StatusPacket {
                raw_id: id,
                flags: StatusFlags(self.buffer[4]),
                data: [0; MAX_STATUS_PARAMETERS],
                data_length,
            };
            packet.data[..usize::from(data_length)].copy_from_slice(&self.buffer[5..total - 1]);
            self.remove_prefix(total);
            report.packets += 1;
            emit(Ok(packet));
        }
    }
}

fn checksum(bytes: &[u8]) -> u8 {
    !bytes.iter().fold(0u8, |sum, byte| sum.wrapping_add(*byte))
}

/// An ID/length/error-checked reply, associated with the request's read window.
/// This is still not proof of time freshness; the wire has no transaction ID.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MatchedReply {
    joint: Joint,
    kind: ReplyKind,
    packet: StatusPacket,
}

impl MatchedReply {
    pub const fn joint(&self) -> Joint {
        self.joint
    }

    pub const fn kind(&self) -> ReplyKind {
        self.kind
    }

    pub fn parameters(&self) -> &[u8] {
        self.packet.parameters()
    }
}

/// One request, at most one successful reply per joint. Any error latches this
/// transaction failed. Parsed later packets cannot silently turn it into success.
#[derive(Clone, Debug)]
pub struct ReplyTracker {
    expected_mask: u8,
    received_mask: u8,
    kind: ReplyKind,
    failure: Option<ReplyError>,
}

impl ReplyTracker {
    pub fn accept(
        &mut self,
        event: Result<StatusPacket, ParseError>,
    ) -> Result<MatchedReply, ReplyError> {
        if let Some(error) = self.failure {
            return Err(error);
        }
        match self.match_reply(event) {
            Ok(reply) => {
                self.received_mask |= reply.joint.mask();
                Ok(reply)
            }
            Err(error) => {
                self.failure = Some(error);
                Err(error)
            }
        }
    }

    fn match_reply(
        &self,
        event: Result<StatusPacket, ParseError>,
    ) -> Result<MatchedReply, ReplyError> {
        let packet = event.map_err(ReplyError::Malformed)?;
        let joint =
            Joint::try_from(packet.raw_id).map_err(|_| ReplyError::UnexpectedId(packet.raw_id))?;
        if self.expected_mask & joint.mask() == 0 {
            return Err(ReplyError::UnexpectedId(packet.raw_id));
        }
        if self.received_mask & joint.mask() != 0 {
            return Err(ReplyError::Duplicate(joint));
        }
        if !packet.flags.is_clear() {
            return Err(ReplyError::ServoFault {
                joint,
                flags: packet.flags,
            });
        }
        let expected = self.kind.parameter_count();
        let received = packet.parameters().len();
        if received != expected {
            return Err(ReplyError::ParameterCount {
                joint,
                expected,
                received,
            });
        }
        Ok(MatchedReply {
            joint,
            kind: self.kind,
            packet,
        })
    }

    pub fn pending_joints(&self) -> impl Iterator<Item = Joint> + '_ {
        Joint::ALL
            .into_iter()
            .filter(|joint| self.expected_mask & !self.received_mask & joint.mask() != 0)
    }

    pub fn failure(&self) -> Option<ReplyError> {
        self.failure
    }

    pub fn is_complete(&self) -> bool {
        self.failure.is_none() && self.received_mask == self.expected_mask
    }

    /// The caller first passes any parser.finish() error to accept(), then calls
    /// this at its deadline. Missing bits use joint ID minus one (low five bits).
    pub fn finish(self) -> Result<(), ReplyError> {
        if let Some(error) = self.failure {
            return Err(error);
        }
        if self.received_mask != self.expected_mask {
            return Err(ReplyError::MissingReplies {
                joint_mask: self.expected_mask & !self.received_mask,
            });
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplyError {
    Malformed(ParseError),
    UnexpectedId(u8),
    Duplicate(Joint),
    ServoFault {
        joint: Joint,
        flags: StatusFlags,
    },
    ParameterCount {
        joint: Joint,
        expected: usize,
        received: usize,
    },
    MissingReplies {
        joint_mask: u8,
    },
}

impl fmt::Display for ReplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Malformed(error) => write!(f, "malformed status: {error}"),
            Self::UnexpectedId(id) => write!(f, "unrequested servo ID {id}"),
            Self::Duplicate(joint) => write!(f, "duplicate reply from {joint}"),
            Self::ServoFault { joint, flags } => {
                write!(f, "{joint} status fault flags {:#04x}", flags.bits())
            }
            Self::ParameterCount {
                joint,
                expected,
                received,
            } => write!(
                f,
                "{joint} replied with {received} parameters, expected {expected}"
            ),
            Self::MissingReplies { joint_mask } => {
                write!(f, "missing servo replies: joint mask {joint_mask:#04x}")
            }
        }
    }
}

impl std::error::Error for ReplyError {}
