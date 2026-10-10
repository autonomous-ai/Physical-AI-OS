//! Sole, bounded ring writer. Control authority is never renewed by frame data.
use crate::{
    ring_wire::{RingBlankReason, RingFeedback, RingFrame, RingPhase, RingRejection},
    transport::WorkerChannels,
    wire::{Control, WorkerEvent},
};
use lamp_interaction::{BootId, BoundaryGuard, Error as AuthorityError, MonoTime, OutputKind};
use lamp_ipc::monotonic_us;
use lamp_ring::{
    BlankReason, BlankReceipt, ChannelCeiling, Failure, FrameSink, GuardedRing, PIXEL_COUNT,
};
use std::{io, time::Duration};

pub const CONTROL_BUDGET: usize = 16;
pub const FRAME_MAX_AGE_US: u64 = 100_000;
pub const INITIAL_AUTHORITY_US: u64 = 250_000;
// The active directed clock starts after this worker. Reserve time for privacy
// worker startup, input readiness and cooperative shutdown. Authority still
// expires independently; this ceiling does not extend any permission lease.
pub const MAX_RUNTIME_US: u64 = (crate::coordinator::MAX_DIRECTED_SECONDS + 30) * 1_000_000;
const TICK_US: u64 = 2_000;

/// Nonblocking operations only; errors, including feedback backpressure, fail
/// closed. This seam permits deterministic cross-channel ordering tests.
pub trait RingLink {
    fn receive_control(&mut self) -> io::Result<Option<Control>>;
    fn receive_frame(&mut self) -> io::Result<Option<RingFrame>>;
    fn publish(&mut self, event: WorkerEvent) -> io::Result<()>;
}

impl RingLink for WorkerChannels {
    fn receive_control(&mut self) -> io::Result<Option<Control>> {
        self.control.receive()
    }
    fn receive_frame(&mut self) -> io::Result<Option<RingFrame>> {
        self.data.receive()
    }
    fn publish(&mut self, event: WorkerEvent) -> io::Result<()> {
        self.control.send(event)
    }
}

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

/// Startup Ready is emitted only after the sink accepted its initial black frame.
/// The caller owns process supervision; a kernel SPI write cannot be preempted.
pub fn run_with_sink(
    mut link: impl RingLink,
    controller_boot: BootId,
    sink: impl FrameSink,
) -> io::Result<()> {
    match GuardedRing::new(sink, controller_boot) {
        Ok(ring) => run_guarded(link, controller_boot, ring),
        Err(error) => report_start_failure(&mut link, io::Error::other(error)),
    }
}

#[cfg(target_os = "linux")]
pub fn run(mut link: WorkerChannels, controller_boot: BootId) -> io::Result<()> {
    match lamp_ring::linux::open(controller_boot) {
        Ok((ring, _configuration)) => run_guarded(link, controller_boot, ring),
        Err(error) => report_start_failure(&mut link, error),
    }
}

fn report_start_failure(link: &mut impl RingLink, error: io::Error) -> io::Result<()> {
    let _ = link.publish(WorkerEvent::Fault {
        code: bounded(&format!("ring startup: {error}")),
    });
    Err(error)
}

fn run_guarded(
    mut link: impl RingLink,
    controller_boot: BootId,
    mut ring: GuardedRing<impl FrameSink>,
) -> io::Result<()> {
    let result = (|| {
        link.publish(WorkerEvent::Ready)?;
        service(&mut link, &mut ring, controller_boot)
    })();
    if let Err(original) = result {
        // GuardedRing already performs its sole cleanup on a write fault. Do
        // not turn that failure into repeated cleanup writes. A transport/control
        // fault gets exactly one control_lost blank, before feedback is attempted.
        let mut detail = format!("ring worker: {original}");
        if !ring.is_faulted() && !ring.is_stopped() {
            match ring.control_lost() {
                Ok(receipt) => {
                    if let Err(error) = blanked(&mut link, receipt) {
                        detail.push_str(&format!("; cleanup receipt: {error}"));
                    }
                }
                Err(error) => detail.push_str(&format!("; cleanup: {error}")),
            }
        }
        let code = bounded(&detail);
        if let Err(error) = link.publish(WorkerEvent::Fault { code: code.clone() }) {
            return Err(io::Error::other(format!("{code}; fault feedback: {error}")));
        }
        return Err(io::Error::other(code));
    }
    Ok(())
}

fn bounded(text: &str) -> String {
    text.chars().take(512).collect()
}

fn blanked(link: &mut impl RingLink, receipt: BlankReceipt) -> io::Result<()> {
    let reason = match receipt.reason {
        BlankReason::Shutdown => RingBlankReason::Shutdown,
        BlankReason::ControlLost => RingBlankReason::ControlLost,
        BlankReason::AuthorityLost(reason) => RingBlankReason::AuthorityLost {
            reason: reason.into(),
        },
        BlankReason::Startup | BlankReason::ExplicitOff => {
            return Err(io::Error::other("unexpected worker blank reason"));
        }
    };
    link.publish(WorkerEvent::Ring {
        report: RingFeedback::Blanked {
            reason,
            write_started_at_us: receipt.write.started_at().as_micros(),
            write_finished_at_us: receipt.write.finished_at().as_micros(),
        },
    })
}

fn reject(link: &mut impl RingLink, frame: RingFrame, reason: RingRejection) -> io::Result<()> {
    link.publish(WorkerEvent::Ring {
        report: RingFeedback::Rejected {
            owner: frame.permit.owner(),
            phase: frame.phase,
            requested_at_us: frame.requested_at_us,
            reason,
        },
    })
}

/// True means Stop completed. Exhausting the shared control budget defers data;
/// it never proves that the queue was empty or permits a frame write.
fn controls(
    link: &mut impl RingLink,
    ring: &mut GuardedRing<impl FrameSink>,
    guard: &mut BoundaryGuard,
    remaining: &mut usize,
) -> io::Result<bool> {
    while *remaining > 0 {
        let Some(command) = link.receive_control()? else {
            return Ok(false);
        };
        *remaining -= 1;
        match command {
            Control::Stop => {
                if let Some(receipt) = ring.shutdown().map_err(io::Error::other)? {
                    blanked(link, receipt)?;
                }
                return Ok(true);
            }
            Control::Authority { snapshot } => {
                if guard.state().is_some_and(|current| {
                    current.revision() == snapshot.revision() && current != snapshot
                }) {
                    return Err(io::Error::other(
                        "ring authority conflicts at same revision",
                    ));
                }
                match guard.install(now(), snapshot) {
                    Ok(()) => {}
                    // These packets cannot extend either installed lease.
                    Err(AuthorityError::StaleSnapshot) => continue,
                    Err(AuthorityError::ExpiredState)
                        if guard
                            .state()
                            .is_some_and(|current| snapshot.revision() <= current.revision()) =>
                    {
                        continue;
                    }
                    // A newer expired state may revoke privacy or ownership.
                    // Never keep displaying under an older allowed lease when
                    // its successor cannot be installed. Cleanup blanks it.
                    Err(error) => return Err(io::Error::other(error)),
                }
                if let Some(receipt) = ring.install_authority(snapshot).map_err(io::Error::other)? {
                    blanked(link, receipt)?;
                }
            }
            Control::StartCapture | Control::ConnectReference { .. } => {
                return Err(io::Error::other("invalid ring control command"));
            }
        }
    }
    Ok(false)
}

fn service(
    link: &mut impl RingLink,
    ring: &mut GuardedRing<impl FrameSink>,
    controller_boot: BootId,
) -> io::Result<()> {
    let started = monotonic_us();
    let mut guard = BoundaryGuard::new(controller_boot, now());
    let mut pending = None;
    loop {
        let tick = monotonic_us();
        let mut budget = CONTROL_BUDGET;
        if controls(link, ring, &mut guard, &mut budget)? {
            return Ok(());
        }
        if let Some(receipt) = ring.tick().map_err(io::Error::other)? {
            blanked(link, receipt)?;
        }
        let at = monotonic_us();
        if at
            .checked_sub(started)
            .is_none_or(|age| age >= MAX_RUNTIME_US)
        {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "ring worker duration expired",
            ));
        }
        let authority_expired = match guard.state() {
            Some(state) => at >= state.expires_at().as_micros(),
            None => at.saturating_sub(started) >= INITIAL_AUTHORITY_US,
        };
        if authority_expired {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "ring authority heartbeat expired",
            ));
        }
        if budget > 0 && pending.is_none() {
            pending = link.receive_frame()?;
        }
        // One shared budget, including controls that arrived during the data
        // receive. A pending frame cannot create another fresh budget or lease.
        if controls(link, ring, &mut guard, &mut budget)? {
            return Ok(());
        }
        if let Some(frame) = pending {
            let at = monotonic_us();
            let reason = if frame.requested_at_us == 0
                || frame.requested_at_us > at
                || frame.permit.issued_at().as_micros() > frame.requested_at_us
                || frame.snapshot.issued_at().as_micros() > frame.requested_at_us
            {
                Some(RingRejection::FutureRequest)
            } else if at - frame.requested_at_us >= FRAME_MAX_AGE_US {
                Some(RingRejection::ExpiredRequest)
            } else if frame.ceiling > 120 {
                Some(RingRejection::InvalidCeiling)
            } else if RingPhase::from_snapshot(frame.snapshot) != Some(frame.phase) {
                Some(RingRejection::PhaseMismatch)
            } else {
                None
            };
            if let Some(reason) = reason {
                pending = None;
                reject(link, frame, reason)?;
            } else if budget > 0
                && let Some(state) = guard.state()
            {
                if frame.snapshot.boot() != controller_boot {
                    pending = None;
                    reject(link, frame, RingRejection::WrongBoot)?;
                } else if frame.snapshot.revision() <= state.revision() {
                    if frame.snapshot.revision() == state.revision() && frame.snapshot != state {
                        return Err(io::Error::other("ring snapshot conflicts at same revision"));
                    }
                    pending = None;
                    // Heartbeats can overtake data. Never install its old state:
                    // require matching owner/phase and check the original permit
                    // against the latest priority authority at the final boundary.
                    if frame.snapshot.owner() != state.owner() {
                        reject(link, frame, RingRejection::StaleOwner)?;
                    } else if RingPhase::from_snapshot(state) != Some(frame.phase) {
                        reject(link, frame, RingRejection::PhaseMismatch)?;
                    } else if let Err(reason) = guard.check(now(), frame.permit, OutputKind::Light)
                    {
                        reject(link, frame, reason.into())?;
                    } else {
                        let pixels = [frame.phase.color(); PIXEL_COUNT];
                        let ceiling =
                            ChannelCeiling::new(frame.ceiling).map_err(io::Error::other)?;
                        match ring.show(&pixels, ceiling, frame.permit) {
                            Ok(write) => link.publish(WorkerEvent::Ring {
                                report: RingFeedback::Presented {
                                    owner: frame.permit.owner(),
                                    phase: frame.phase,
                                    requested_at_us: frame.requested_at_us,
                                    write_started_at_us: write.started_at().as_micros(),
                                    write_finished_at_us: write.finished_at().as_micros(),
                                },
                            })?,
                            Err(error) => match error.cause {
                                Failure::Authority(reason)
                                    if error.cleanup_error.is_none() && !ring.is_faulted() =>
                                {
                                    // A deadline can cross the final device check.
                                    // GuardedRing reconciles it; no fabricated
                                    // successful blank/write timestamp is emitted.
                                    reject(link, frame, reason.into())?;
                                }
                                _ => return Err(io::Error::other(error)),
                            },
                        }
                    }
                }
            }
        }
        if let Some(wait) = TICK_US.checked_sub(monotonic_us().saturating_sub(tick)) {
            std::thread::sleep(Duration::from_micros(wait));
        }
    }
}
