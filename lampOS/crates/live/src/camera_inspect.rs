//! Finite camera qualification metadata; never an image or social inference.
use crate::{
    camera_config::CameraConfig,
    camera_worker::{
        CameraEvent, FrameMetadata, Mode, PROGRESS_DEADLINE_US, PortMetadata, StopReason, Timing,
    },
};
use lamp_interaction::{BootId, CameraGrant};
use serde::Serialize;
use std::io;

mod supervision;
pub use supervision::run;

pub const MAX_INSPECTION_SECONDS: u64 = 60;
pub const MAX_FRAME_SAMPLES: usize = 64;
pub const METADATA_FRESH_US: u64 = 100_000;

#[derive(Default, Debug, Serialize)]
pub struct LatencySummary {
    pub count: u64,
    pub min_us: Option<u64>,
    pub max_us: Option<u64>,
    pub sum_us: u64,
}
impl LatencySummary {
    fn add(&mut self, value: u64) -> io::Result<()> {
        self.count = self
            .count
            .checked_add(1)
            .ok_or_else(|| io::Error::other("metadata counter overflow"))?;
        self.sum_us = self
            .sum_us
            .checked_add(value)
            .ok_or_else(|| io::Error::other("metadata timing sum overflow"))?;
        self.min_us = Some(self.min_us.map_or(value, |old| old.min(value)));
        self.max_us = Some(self.max_us.map_or(value, |old| old.max(value)));
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Serialize)]
pub struct PrivacyObservation {
    pub sequence: u64,
    pub acquired_at_us: u64,
    pub received_at_us: u64,
    pub muted: bool,
}
#[derive(Serialize)]
pub struct MetadataReport {
    pub schema: u32,
    pub config_sha256: String,
    pub requested: CameraConfig,
    pub requested_seconds: u64,
    pub started_at_us: u64,
    pub ended_at_us: Option<u64>,
    pub outcome: String,
    pub error: Option<String>,
    pub capture_started: Option<CameraEvent>,
    pub first_frame_at_us: Option<u64>,
    pub frames_accepted: u64,
    pub frame_sequence_gaps: u64,
    pub first_frame_samples: Vec<FrameMetadata>,
    pub omitted_frame_samples: u64,
    pub dequeue_to_worker_handoff: LatencySummary,
    pub dequeue_to_parent_acceptance: LatencySummary,
    pub final_worker_event: Option<CameraEvent>,
    pub shutdown: Option<ShutdownReport>,
    pub last_physical_privacy: Option<PrivacyObservation>,
    pub parent_privacy_revoke_at_us: Option<u64>,
    pub images_saved: bool,
    pub images_transmitted: bool,
    pub optical_visibility_qualified: bool,
    pub listening_readiness_claimed: bool,
}
#[derive(Default, Serialize)]
pub struct ShutdownReport {
    pub requested_at_us: u64,
    pub camera_exit_at_us: Option<u64>,
    pub privacy_exit_at_us: Option<u64>,
    pub camera_forced: bool,
    pub privacy_forced: bool,
    pub errors: Vec<String>,
    pub omitted_errors: u64,
}
impl ShutdownReport {
    fn record_error(&mut self, mut error: String) {
        // Cleanup must not turn repeated malformed datagrams into an unbounded
        // diagnostic queue. Preserve the failure and count omitted evidence.
        if self.errors.len() >= 16 {
            self.omitted_errors = self.omitted_errors.saturating_add(1);
            return;
        }
        if error.len() > 1024 {
            let mut end = 1024;
            while !error.is_char_boundary(end) {
                end -= 1;
            }
            error.truncate(end);
            error.push_str(" [truncated]");
        }
        self.errors.push(error);
    }
}
impl MetadataReport {
    pub fn new(
        config: CameraConfig,
        digest: String,
        seconds: u64,
        now_us: u64,
    ) -> io::Result<Self> {
        if !(1..=MAX_INSPECTION_SECONDS).contains(&seconds) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "camera inspection duration must be 1..=60 seconds",
            ));
        }
        config.capture()?;
        Ok(Self {
            schema: 1,
            config_sha256: digest,
            requested: config,
            requested_seconds: seconds,
            started_at_us: now_us,
            ended_at_us: None,
            outcome: "incomplete".into(),
            error: None,
            capture_started: None,
            first_frame_at_us: None,
            frames_accepted: 0,
            frame_sequence_gaps: 0,
            first_frame_samples: Vec::with_capacity(MAX_FRAME_SAMPLES),
            omitted_frame_samples: 0,
            dequeue_to_worker_handoff: LatencySummary::default(),
            dequeue_to_parent_acceptance: LatencySummary::default(),
            final_worker_event: None,
            shutdown: None,
            last_physical_privacy: None,
            parent_privacy_revoke_at_us: None,
            images_saved: false,
            images_transmitted: false,
            optical_visibility_qualified: false,
            listening_readiness_claimed: false,
        })
    }
}
/// Parent-side independent metadata validation. Physical privacy is checked
/// immediately before this call; a None grant rejects any frame, including one
/// already dequeued before revoke. A new grant cannot relabel that observation.
pub struct Observer {
    worker: BootId,
    deadline_us: u64,
    opening: Option<u64>,
    mode: Option<Mode>,
    epoch: Option<u64>,
    last_sequence: u64,
    last_driver_sequence: Option<u32>,
    stopped: bool,
}
impl Observer {
    pub fn new(worker: BootId, now_us: u64) -> Self {
        Self {
            worker,
            deadline_us: now_us.saturating_add(PROGRESS_DEADLINE_US),
            opening: None,
            mode: None,
            epoch: None,
            last_sequence: 0,
            last_driver_sequence: None,
            stopped: false,
        }
    }
    pub fn check_deadline(&self, now_us: u64) -> io::Result<()> {
        if !self.stopped && now_us >= self.deadline_us {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "camera worker operation/progress deadline expired",
            ));
        }
        Ok(())
    }
    pub fn accept(
        &mut self,
        event: CameraEvent,
        grant: Option<CameraGrant>,
        now_us: u64,
        report: &mut MetadataReport,
    ) -> io::Result<()> {
        self.check_deadline(now_us)?;
        if self.stopped {
            return Err(io::Error::other("camera metadata arrived after stop"));
        }
        let progress_deadline = match &event {
            CameraEvent::Started { at_us, .. } | CameraEvent::Progress { at_us, .. } => {
                Some(*at_us)
            }
            CameraEvent::Frame { frame } => Some(frame.published_at_us),
            _ => None,
        }
        .map(|produced| {
            produced
                .checked_add(PROGRESS_DEADLINE_US)
                .ok_or_else(|| io::Error::other("camera progress deadline overflow"))
        })
        .transpose()?;
        if progress_deadline.is_some_and(|deadline| now_us >= deadline) {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "queued camera metadata cannot revive stalled progress",
            ));
        }
        match &event {
            CameraEvent::Opening { at_us, deadline_us } => {
                fresh(*at_us, now_us)?;
                if self.opening.is_some()
                    || self.mode.is_some()
                    || grant.is_none()
                    || at_us.checked_add(lamp_camera::START_BUDGET_US) != Some(*deadline_us)
                {
                    return Err(io::Error::other("invalid/repeated camera opening metadata"));
                }
                self.opening = Some(*at_us);
                self.deadline_us = deadline_us
                    .checked_add(lamp_camera::POLL_TARGET_US)
                    .ok_or_else(|| io::Error::other("camera deadline overflow"))?;
                return Ok(());
            }
            CameraEvent::Started {
                at_us,
                worker,
                capture_epoch,
                mode,
                timing,
                port,
            } => {
                fresh(*at_us, now_us)?;
                if *worker != self.worker
                    || *capture_epoch != 1
                    || self.mode.is_some()
                    || grant.is_none()
                    || self
                        .opening
                        .is_none_or(|opened| timing.started_at_us < opened)
                    || timing.completed_at_us > *at_us
                {
                    return Err(io::Error::other("invalid camera start lineage/timing"));
                }
                valid_timing(*timing, lamp_camera::START_BUDGET_US)?;
                check_mode(mode, &report.requested)?;
                check_identity(port, &report.requested)?;
                self.mode = Some(mode.clone());
                self.epoch = Some(*capture_epoch);
                self.opening = None;
                report.capture_started = Some(event.clone());
            }
            CameraEvent::Frame { frame } => {
                fresh(frame.published_at_us, now_us)?;
                if grant != Some(frame.grant)
                    || frame.worker != self.worker
                    || self.epoch != Some(frame.capture_epoch)
                    || self.mode.as_ref() != Some(&frame.mode)
                    || frame.sequence <= self.last_sequence
                    || frame.bytes_used < 4
                    || frame.bytes_used > frame.mode.size_image
                    || frame.dequeue.completed_at_us > frame.published_at_us
                    || now_us
                        .checked_sub(frame.dequeue.completed_at_us)
                        .is_none_or(|age| age >= lamp_camera::MAX_DEQUEUE_AGE_US)
                {
                    return Err(io::Error::other(
                        "stale or incompatible camera frame metadata",
                    ));
                }
                valid_timing(frame.dequeue, lamp_camera::READ_BUDGET_US)?;
                if let Some(previous) = self.last_driver_sequence {
                    let delta = frame.driver_sequence.wrapping_sub(previous);
                    if delta == 0 || delta >= 1 << 31 {
                        return Err(io::Error::other("camera driver sequence regressed"));
                    }
                }
                report.frame_sequence_gaps = report
                    .frame_sequence_gaps
                    .checked_add(frame.sequence - self.last_sequence - 1)
                    .ok_or_else(|| io::Error::other("frame gap counter overflow"))?;
                self.last_sequence = frame.sequence;
                self.last_driver_sequence = Some(frame.driver_sequence);
                report.frames_accepted = report
                    .frames_accepted
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("frame count overflow"))?;
                report.first_frame_at_us.get_or_insert(now_us);
                report
                    .dequeue_to_worker_handoff
                    .add(frame.published_at_us - frame.dequeue.completed_at_us)?;
                report
                    .dequeue_to_parent_acceptance
                    .add(now_us - frame.dequeue.completed_at_us)?;
                if report.first_frame_samples.len() < MAX_FRAME_SAMPLES {
                    report.first_frame_samples.push(frame.clone());
                } else {
                    report.omitted_frame_samples += 1;
                }
            }
            CameraEvent::Progress { at_us, .. } => {
                fresh(*at_us, now_us)?;
                if self.opening.is_some() {
                    return Err(io::Error::other(
                        "progress cannot extend an unfinished camera start",
                    ));
                }
            }
            CameraEvent::Stopped {
                at_us,
                reason,
                error,
                ..
            } => {
                fresh(*at_us, now_us)?;
                report.final_worker_event = Some(event.clone());
                self.stopped = true;
                return Err(io::Error::other(format!(
                    "camera stopped before inspection end: {reason:?}, {error:?}"
                )));
            }
        }
        // Use production time, never receipt of an old queued packet.
        self.deadline_us = progress_deadline.expect("opening and stop return above");
        Ok(())
    }
}
fn fresh(at_us: u64, now_us: u64) -> io::Result<()> {
    if now_us
        .checked_sub(at_us)
        .is_none_or(|age| age >= METADATA_FRESH_US)
    {
        Err(io::Error::other("camera metadata time is stale/future"))
    } else {
        Ok(())
    }
}
fn valid_timing(timing: Timing, max: u64) -> io::Result<()> {
    if timing
        .completed_at_us
        .checked_sub(timing.started_at_us)
        .is_none_or(|elapsed| elapsed >= max)
    {
        Err(io::Error::other(
            "invalid or over-budget camera operation timing",
        ))
    } else {
        Ok(())
    }
}
fn check_mode(mode: &Mode, config: &CameraConfig) -> io::Result<()> {
    let requested = config.capture()?;
    let interval_valid = match (requested.interval, mode.interval) {
        (Some(wanted), Some(actual)) => {
            actual.checked()?.numerator() as u64 * wanted.denominator() as u64
                == wanted.numerator() as u64 * actual.checked()?.denominator() as u64
        }
        (Some(_), None) => false,
        (None, Some(actual)) => {
            actual.checked()?;
            true
        }
        (None, None) => true,
    };
    if mode.source != config.source
        || mode.format != *b"MJPG"
        || mode.width != config.width
        || mode.height != config.height
        || !(4..=config.max_frame_bytes).contains(&mode.size_image)
        || !(2..=config.buffers).contains(&mode.buffers)
        || !interval_valid
    {
        return Err(io::Error::other(
            "camera metadata reports incompatible negotiation",
        ));
    }
    Ok(())
}
fn check_identity(port: &PortMetadata, config: &CameraConfig) -> io::Result<()> {
    let Some(identity) = &port.identity else {
        return Err(io::Error::other("camera start lacks checked USB identity"));
    };
    let actual = &identity.usb;
    let expected = &config.expected_usb;
    if actual.vendor != expected.vendor
        || actual.product != expected.product
        || actual.topology != expected.topology
        || actual.interface_number != expected.interface_number
        || actual.capture_index != expected.capture_index
        || expected
            .serial
            .as_ref()
            .is_some_and(|s| actual.serial.as_ref() != Some(s))
        || !crate::camera_config::valid_absolute(std::path::Path::new(&identity.canonical_node))
    {
        return Err(io::Error::other("camera metadata USB identity mismatch"));
    }
    Ok(())
}

/// Shutdown data may include prior valid metadata still in flight. It is never
/// counted as a newly accepted frame after revocation; only Stopped is retained.
pub fn retain_stop(event: CameraEvent, report: &mut MetadataReport) -> io::Result<()> {
    if let CameraEvent::Stopped { reason, error, .. } = &event {
        if report.final_worker_event.is_some() {
            return Err(io::Error::other("duplicate camera stop report"));
        }
        let failed = *reason == StopReason::Fault || error.is_some();
        report.final_worker_event = Some(event);
        if failed {
            return Err(io::Error::other("camera reported failed cleanup"));
        }
    }
    Ok(())
}
