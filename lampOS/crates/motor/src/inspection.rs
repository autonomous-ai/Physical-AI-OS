//! Finite, read-only inspection plan and bounded transaction engine.
//!
//! The private I/O boundary is used by the Linux owner and synthetic tests only.
//! Public callers cannot inject instruction bytes. A quiet interval is evidence
//! of observed silence, never proof that old adapter/servo traffic cannot arrive.

use crate::{
    Joint, STS3215_MODEL_NUMBER,
    protocol::{MatchedReply, ReadOnlyRequest, ReadWindow, ReplyError, StatusParser},
};
use serde::Serialize;
use std::{io, time::Duration};

pub const TRANSACTION_DEADLINE: Duration = Duration::from_millis(200);
pub const TELEMETRY_TARGET: Duration = Duration::from_millis(25);
pub const INITIAL_QUIET: Duration = Duration::from_millis(20);
pub const POLL_SLICE: Duration = Duration::from_millis(2);
const MAX_OPERATIONS: u16 = 512;
const READ_BYTES: usize = 64;
const MAX_INITIAL_DISCARD: u16 = 4096;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Ping1,
    Ping2,
    Ping3,
    Ping4,
    Ping5,
    Model,
    Limits,
    HomingMode,
    Telemetry,
}

impl Stage {
    const PLAN: [Self; 9] = [
        Self::Ping1,
        Self::Ping2,
        Self::Ping3,
        Self::Ping4,
        Self::Ping5,
        Self::Model,
        Self::Limits,
        Self::HomingMode,
        Self::Telemetry,
    ];

    fn request(self) -> ReadOnlyRequest {
        let window = match self {
            Self::Ping1 => return ReadOnlyRequest::ping(Joint::BaseYaw),
            Self::Ping2 => return ReadOnlyRequest::ping(Joint::BasePitch),
            Self::Ping3 => return ReadOnlyRequest::ping(Joint::ElbowPitch),
            Self::Ping4 => return ReadOnlyRequest::ping(Joint::WristRoll),
            Self::Ping5 => return ReadOnlyRequest::ping(Joint::WristPitch),
            Self::Model => ReadWindow::MODEL_NUMBER,
            Self::Limits => ReadWindow::POSITION_LIMITS,
            Self::HomingMode => ReadWindow::HOMING_AND_MODE,
            Self::Telemetry => ReadWindow::TELEMETRY,
        };
        ReadOnlyRequest::sync_read(&Joint::ALL, window)
            .expect("the fixed five joint identities are unique")
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Fault {
    Cancelled,
    CancellationIo {
        detail: String,
    },
    ClockRegression,
    Deadline {
        pending_ids: Vec<u8>,
        partial_bytes: usize,
    },
    OperationLimit,
    InitialTrafficLimit {
        discarded_bytes: u16,
    },
    UnexpectedPriorTraffic {
        bytes: usize,
    },
    Noise {
        bytes: usize,
    },
    TrailingPartial {
        bytes: usize,
    },
    Protocol {
        detail: String,
    },
    Decode {
        detail: String,
    },
    UnexpectedModel {
        joint_id: u8,
        model_raw: u16,
    },
    Io {
        operation: &'static str,
        code: Option<i32>,
        detail: String,
    },
    Disconnected,
    WriteZero,
    InvalidIoCount,
}

impl Fault {
    fn io(operation: &'static str, error: io::Error) -> Self {
        Self::Io {
            operation,
            code: error.raw_os_error(),
            detail: limited(error.to_string()),
        }
    }
}

fn limited(value: String) -> String {
    value.chars().take(192).collect()
}

#[derive(Clone, Debug, Serialize)]
pub struct JointReadback {
    pub joint_id: u8,
    pub host_read_completed_us: u64,
    pub data: Readback,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Readback {
    Ping,
    Model {
        number_raw: u16,
    },
    Limits {
        min_raw: u16,
        max_raw: u16,
    },
    HomingMode {
        homing_word: u16,
        homing_counts: i16,
        operating_mode_raw: u8,
    },
    Telemetry {
        register_bytes: [u8; 15],
        position_raw: u16,
        velocity_word: u16,
        velocity_signed_raw: i16,
        load_word: u16,
        load_magnitude_raw: u16,
        load_direction_bit: bool,
        load_uninterpreted_bits: u16,
        voltage_raw: u8,
        temperature_raw: u8,
        status_raw: u8,
        moving_raw: u8,
        current_raw: u16,
    },
}

fn decode(stage: Stage, reply: MatchedReply, read_at: u64) -> Result<JointReadback, Fault> {
    let joint_id = reply.joint().id();
    let decode_error = |error: crate::readback::DecodeError| Fault::Decode {
        detail: limited(error.to_string()),
    };
    let data = match stage {
        Stage::Ping1 | Stage::Ping2 | Stage::Ping3 | Stage::Ping4 | Stage::Ping5 => Readback::Ping,
        Stage::Model => {
            let number_raw = reply.model_number_raw().map_err(decode_error)?;
            if number_raw != STS3215_MODEL_NUMBER {
                return Err(Fault::UnexpectedModel {
                    joint_id,
                    model_raw: number_raw,
                });
            }
            Readback::Model { number_raw }
        }
        Stage::Limits => {
            let limits = reply.position_limits().map_err(decode_error)?;
            Readback::Limits {
                min_raw: limits.min().raw(),
                max_raw: limits.max().raw(),
            }
        }
        Stage::HomingMode => {
            let value = reply.homing_and_mode().map_err(decode_error)?;
            Readback::HomingMode {
                homing_word: value.homing_word(),
                homing_counts: value.homing_offset().get(),
                operating_mode_raw: value.operating_mode_raw(),
            }
        }
        Stage::Telemetry => {
            let value = reply.telemetry().map_err(decode_error)?;
            Readback::Telemetry {
                register_bytes: *value.bytes(),
                position_raw: value.position().raw(),
                velocity_word: value.velocity().word(),
                velocity_signed_raw: value.velocity().signed_raw(),
                load_word: value.load().word(),
                load_magnitude_raw: value.load().magnitude_raw(),
                load_direction_bit: value.load().direction_bit(),
                load_uninterpreted_bits: value.load().uninterpreted_bits(),
                voltage_raw: value.voltage_raw(),
                temperature_raw: value.temperature_raw(),
                status_raw: value.status_raw(),
                moving_raw: value.moving_raw(),
                current_raw: value.current_raw(),
            }
        }
    };
    Ok(JointReadback {
        joint_id,
        host_read_completed_us: read_at,
        data,
    })
}

#[derive(Clone, Debug, Serialize)]
pub struct TransactionReport {
    pub stage: Stage,
    pub started_us: u64,
    pub first_write_attempt_us: Option<u64>,
    /// Last request byte accepted by the host driver, not wire delivery proof.
    pub request_accepted_us: Option<u64>,
    pub first_read_completed_us: Option<u64>,
    pub ended_us: u64,
    pub elapsed_us: u64,
    pub request_bytes_accepted: usize,
    pub bytes_read: usize,
    pub io_operations: u16,
    pub pending_ids: Vec<u8>,
    /// At most five, acquired during this transaction. A failed transaction's
    /// partial observations are diagnostic data, not a successful snapshot.
    pub replies: Vec<JointReadback>,
    pub fault: Option<Fault>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct QuietReport {
    pub started_us: u64,
    pub ended_us: u64,
    pub discarded_bytes: u16,
    pub io_operations: u16,
    pub observed_quiet_us: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct InspectionReport {
    pub completed: bool,
    pub freshness: &'static str,
    pub transaction_deadline_us: u64,
    pub telemetry_target_us: u64,
    pub telemetry_target_met: Option<bool>,
    pub quiet: QuietReport,
    /// Nine maximum. No transaction is retried after failure.
    pub transactions: Vec<TransactionReport>,
    pub fault: Option<Fault>,
}

#[derive(Clone, Copy)]
pub(crate) enum Interest {
    Read,
    Write,
}

/// Private so no hardware writer accepting arbitrary bytes is publicly exposed.
pub(crate) trait PortIo {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize>;
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize>;
    fn wait(&mut self, interest: Interest, maximum: Duration) -> io::Result<()>;
}

pub(crate) trait Runtime {
    fn now(&self) -> Duration;
    fn cancelled(&mut self) -> io::Result<bool>;
}

fn micros(time: Duration) -> u64 {
    time.as_micros().try_into().unwrap_or(u64::MAX)
}

struct Clock {
    last: Duration,
}
impl Clock {
    fn observe(&mut self, runtime: &mut impl Runtime) -> Result<Duration, Fault> {
        let cancelled = runtime.cancelled().map_err(|error| Fault::CancellationIo {
            detail: limited(error.to_string()),
        })?;
        let now = runtime.now();
        if now < self.last {
            return Err(Fault::ClockRegression);
        }
        self.last = now;
        if cancelled {
            return Err(Fault::Cancelled);
        }
        Ok(now)
    }

    fn check(
        &mut self,
        runtime: &mut impl Runtime,
        deadline: Duration,
        pending_ids: &[u8],
        partial_bytes: usize,
    ) -> Result<Duration, Fault> {
        let now = self.observe(runtime)?;
        if now >= deadline {
            return Err(Fault::Deadline {
                pending_ids: pending_ids.to_vec(),
                partial_bytes,
            });
        }
        Ok(now)
    }
}

fn operation(count: &mut u16) -> Result<(), Fault> {
    if *count >= MAX_OPERATIONS {
        return Err(Fault::OperationLimit);
    }
    *count += 1;
    Ok(())
}

fn pending(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}

fn quiet(
    io: &mut impl PortIo,
    runtime: &mut impl Runtime,
    clock: &mut Clock,
    report: &mut QuietReport,
) -> Result<(), Fault> {
    let start = clock.observe(runtime)?;
    report.started_us = micros(start);
    let deadline = start.saturating_add(TRANSACTION_DEADLINE);
    let mut last_data = start;
    let mut bytes = [0; READ_BYTES];
    loop {
        clock.check(runtime, deadline, &[], 0)?;
        operation(&mut report.io_operations)?;
        let result = io.read(&mut bytes);
        let after = clock.check(runtime, deadline, &[], 0)?;
        match result {
            Ok(0) => return Err(Fault::Disconnected),
            Ok(count) if count <= bytes.len() => {
                report.discarded_bytes = report.discarded_bytes.saturating_add(count as u16);
                if report.discarded_bytes > MAX_INITIAL_DISCARD {
                    return Err(Fault::InitialTrafficLimit {
                        discarded_bytes: report.discarded_bytes,
                    });
                }
                last_data = after;
            }
            Ok(_) => return Err(Fault::InvalidIoCount),
            Err(error) if pending(&error) => {
                // EINTR is not evidence of an empty driver queue.
                if error.kind() == io::ErrorKind::WouldBlock
                    && after.saturating_sub(last_data) >= INITIAL_QUIET
                {
                    report.observed_quiet_us = micros(after.saturating_sub(last_data));
                    report.ended_us = micros(after);
                    return Ok(());
                }
            }
            Err(error) => return Err(Fault::io("initial_read", error)),
        }
        operation(&mut report.io_operations)?;
        let wait = POLL_SLICE
            .min(deadline.saturating_sub(after))
            .min(INITIAL_QUIET);
        match io.wait(Interest::Read, wait) {
            Ok(()) => {}
            Err(error) if pending(&error) => {}
            Err(error) => return Err(Fault::io("initial_poll", error)),
        }
        clock.check(runtime, deadline, &[], 0)?;
    }
}

fn transact(
    io: &mut impl PortIo,
    runtime: &mut impl Runtime,
    clock: &mut Clock,
    stage: Stage,
) -> TransactionReport {
    let start = runtime.now();
    let mut report = TransactionReport {
        stage,
        started_us: micros(start),
        first_write_attempt_us: None,
        request_accepted_us: None,
        first_read_completed_us: None,
        ended_us: micros(start),
        elapsed_us: 0,
        request_bytes_accepted: 0,
        bytes_read: 0,
        io_operations: 0,
        pending_ids: Vec::new(),
        replies: Vec::with_capacity(5),
        fault: None,
    };
    let result = transaction_body(
        io,
        runtime,
        clock,
        &mut report,
        start.saturating_add(TRANSACTION_DEADLINE),
    );
    // Keep the latest observed host time even when cancellation/deadline failed.
    report.ended_us = micros(clock.last);
    report.elapsed_us = report.ended_us.saturating_sub(report.started_us);
    report.fault = result.err();
    report
}

fn transaction_body(
    io: &mut impl PortIo,
    runtime: &mut impl Runtime,
    clock: &mut Clock,
    report: &mut TransactionReport,
    deadline: Duration,
) -> Result<(), Fault> {
    let request = report.stage.request();
    let mut tracker = request.track_replies();
    let mut parser = StatusParser::default();
    let mut bytes = [0; READ_BYTES];
    report.pending_ids = tracker.pending_joints().map(Joint::id).collect();
    // Before every partial write, any readable byte belongs to prior traffic.
    // The servo cannot legitimately reply to an incomplete checksum frame.
    while report.request_bytes_accepted < request.bytes().len() {
        clock.check(runtime, deadline, &report.pending_ids, 0)?;
        operation(&mut report.io_operations)?;
        let probe = io.read(&mut bytes);
        clock.check(runtime, deadline, &report.pending_ids, 0)?;
        match probe {
            Ok(0) => return Err(Fault::Disconnected),
            Ok(count) if count <= bytes.len() => {
                return Err(Fault::UnexpectedPriorTraffic { bytes: count });
            }
            Ok(_) => return Err(Fault::InvalidIoCount),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(Fault::io("pre_write_read", error)),
        }
        let before = clock.check(runtime, deadline, &report.pending_ids, 0)?;
        operation(&mut report.io_operations)?;
        report.first_write_attempt_us.get_or_insert(micros(before));
        let remaining = &request.bytes()[report.request_bytes_accepted..];
        let write = io.write(remaining);
        if let Ok(count) = &write {
            if *count > remaining.len() {
                return Err(Fault::InvalidIoCount);
            }
            report.request_bytes_accepted += count;
        }
        let after = clock.check(runtime, deadline, &report.pending_ids, 0)?;
        match write {
            Ok(0) => return Err(Fault::WriteZero),
            Ok(_) => {}
            Err(error) if pending(&error) => {
                operation(&mut report.io_operations)?;
                match io.wait(
                    Interest::Write,
                    POLL_SLICE.min(deadline.saturating_sub(after)),
                ) {
                    Ok(()) => {}
                    Err(error) if pending(&error) => {}
                    Err(error) => return Err(Fault::io("write_poll", error)),
                }
                clock.check(runtime, deadline, &report.pending_ids, 0)?;
            }
            Err(error) => return Err(Fault::io("write", error)),
        }
    }
    report.request_accepted_us = Some(micros(clock.last));
    loop {
        clock.check(
            runtime,
            deadline,
            &report.pending_ids,
            parser.buffered_bytes(),
        )?;
        operation(&mut report.io_operations)?;
        let read = io.read(&mut bytes);
        if let Ok(count) = &read {
            if *count > bytes.len() {
                return Err(Fault::InvalidIoCount);
            }
            report.bytes_read += count;
        }
        let read_at = clock.check(
            runtime,
            deadline,
            &report.pending_ids,
            parser.buffered_bytes(),
        )?;
        match read {
            Ok(0) => return Err(Fault::Disconnected),
            Ok(count) => {
                report
                    .first_read_completed_us
                    .get_or_insert(micros(read_at));
                let mut error = None;
                let parsed = parser.feed(&bytes[..count], |event| {
                    if error.is_some() {
                        return;
                    }
                    match tracker
                        .accept(event)
                        .map_err(protocol_fault)
                        .and_then(|reply| decode(report.stage, reply, micros(read_at)))
                    {
                        Ok(reply) => report.replies.push(reply),
                        Err(fault) => error = Some(fault),
                    }
                });
                report.pending_ids = tracker.pending_joints().map(Joint::id).collect();
                if let Some(error) = error {
                    return Err(error);
                }
                if parsed.discarded_bytes != 0 {
                    return Err(Fault::Noise {
                        bytes: parsed.discarded_bytes,
                    });
                }
                clock.check(
                    runtime,
                    deadline,
                    &report.pending_ids,
                    parser.buffered_bytes(),
                )?;
                if tracker.is_complete() {
                    if parser.buffered_bytes() != 0 {
                        return Err(Fault::TrailingPartial {
                            bytes: parser.buffered_bytes(),
                        });
                    }
                    tracker.finish().map_err(protocol_fault)?;
                    return Ok(());
                }
            }
            Err(error) if pending(&error) => {}
            Err(error) => return Err(Fault::io("read", error)),
        }
        operation(&mut report.io_operations)?;
        match io.wait(
            Interest::Read,
            POLL_SLICE.min(deadline.saturating_sub(read_at)),
        ) {
            Ok(()) => {}
            Err(error) if pending(&error) => {}
            Err(error) => return Err(Fault::io("read_poll", error)),
        }
        clock.check(
            runtime,
            deadline,
            &report.pending_ids,
            parser.buffered_bytes(),
        )?;
    }
}

fn protocol_fault(error: ReplyError) -> Fault {
    Fault::Protocol {
        detail: limited(error.to_string()),
    }
}

pub(crate) fn run(io: &mut impl PortIo, runtime: &mut impl Runtime) -> InspectionReport {
    let mut report = InspectionReport {
        completed: false,
        freshness: "not_proven_wire_has_no_transaction_id",
        transaction_deadline_us: micros(TRANSACTION_DEADLINE),
        telemetry_target_us: micros(TELEMETRY_TARGET),
        telemetry_target_met: None,
        quiet: QuietReport::default(),
        transactions: Vec::with_capacity(9),
        fault: None,
    };
    let mut clock = Clock {
        last: Duration::ZERO,
    };
    if let Err(error) = quiet(io, runtime, &mut clock, &mut report.quiet) {
        report.quiet.ended_us = micros(clock.last);
        report.fault = Some(error);
        return report;
    }
    for stage in Stage::PLAN {
        let transaction = transact(io, runtime, &mut clock, stage);
        let failure = transaction.fault.clone();
        if stage == Stage::Telemetry && failure.is_none() {
            report.telemetry_target_met = Some(transaction.elapsed_us <= micros(TELEMETRY_TARGET));
        }
        report.transactions.push(transaction);
        if let Some(error) = failure {
            report.fault = Some(error);
            return report;
        }
    }
    report.completed = true;
    report
}

#[cfg(test)]
mod tests;
