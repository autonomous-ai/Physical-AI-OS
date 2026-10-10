//! Bounded retention of successful static-cue lease maintenance, not output policy.
//!
//! The coordinator still sends and validates every frame. The first request of
//! each cue and each second remains a normal trace event. Only intervening
//! same-cue renewals share a summary and a raw tail-sample slot. The summary records
//! submitted versus acknowledged counts, including a final unacknowledged frame.
use super::{Result, record};
use crate::ring_wire::{RingFeedback, RingFrame};
use lamp_interaction::{BoundaryGuard, MonoTime, OutputKind};
use serde_json::{Value, json};
use std::io;

const SUMMARY_INTERVAL_US: u64 = 1_000_000;
const WRITE_BOUNDARY: &str = "SPI write return; optical output unmeasured";

#[derive(Default)]
pub(super) struct RingTrace {
    confirmed: Option<RingFrame>,
    anchor: Option<RingFrame>,
    pending: Option<Pending>,
    renewals: Option<Renewals>,
}

#[derive(Clone, Copy)]
struct Pending {
    frame: RingFrame,
    aggregated: bool,
}

struct Renewals {
    index: usize,
    tail_index: usize,
    anchor_requested_at_us: u64,
    first: RingFrame,
    last: RingFrame,
    requests: u32,
    presentations: u32,
    rejections: u32,
    first_presented: Option<RingFeedback>,
    last_presented: Option<RingFeedback>,
    latency_min_us: Option<u64>,
    latency_max_us: Option<u64>,
    latency_total_us: u64,
    updated_at_us: u64,
}

impl RingTrace {
    pub(super) fn requested(&mut self, frame: RingFrame, trace: &mut Vec<Value>) -> Result<()> {
        if self.pending.is_some() {
            return Err(io::Error::other("ring trace has an outstanding request").into());
        }
        let aggregate = self.confirmed.is_some_and(|last| same_cue(last, frame))
            && self.anchor.is_some_and(|anchor| {
                frame
                    .requested_at_us
                    .checked_sub(anchor.requested_at_us)
                    .is_some_and(|elapsed| elapsed < SUMMARY_INTERVAL_US)
            });
        if aggregate {
            if let Some(batch) = self.renewals.as_mut() {
                batch.requests = batch
                    .requests
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("ring renewal request counter exhausted"))?;
                batch.last = frame;
                batch.updated_at_us = frame.requested_at_us;
                batch.update(trace)?;
            } else {
                let batch = Renewals {
                    index: trace.len(),
                    tail_index: trace.len() + 1,
                    anchor_requested_at_us: self
                        .anchor
                        .ok_or_else(|| io::Error::other("ring renewal has no anchor"))?
                        .requested_at_us,
                    first: frame,
                    last: frame,
                    requests: 1,
                    presentations: 0,
                    rejections: 0,
                    first_presented: None,
                    last_presented: None,
                    latency_min_us: None,
                    latency_max_us: None,
                    latency_total_us: 0,
                    updated_at_us: frame.requested_at_us,
                };
                record(trace, batch.event())?;
                record(trace, request_event(frame, "renewal_tail"))?;
                self.renewals = Some(batch);
            }
        } else {
            record(trace, request_event(frame, "cue_or_periodic_head"))?;
            self.anchor = Some(frame);
            self.renewals = None;
        }
        self.pending = Some(Pending {
            frame,
            aggregated: aggregate,
        });
        Ok(())
    }

    /// `validation_error == None` is supplied only after the real choreographer
    /// accepted this exact receipt. Faults, blanks and rejections stay individual.
    pub(super) fn feedback(
        &mut self,
        report: RingFeedback,
        observed_at_us: u64,
        validation_error: Option<&str>,
        trace: &mut Vec<Value>,
    ) -> Result<()> {
        let matched = self.pending.filter(|pending| match report {
            RingFeedback::Presented {
                owner,
                phase,
                requested_at_us,
                ..
            }
            | RingFeedback::Rejected {
                owner,
                phase,
                requested_at_us,
                ..
            } => {
                (owner, phase, requested_at_us)
                    == (
                        pending.frame.permit.owner(),
                        pending.frame.phase,
                        pending.frame.requested_at_us,
                    )
            }
            RingFeedback::Blanked { .. } => false,
        });
        let aggregate = validation_error.is_none()
            && matched.is_some_and(|pending| pending.aggregated)
            && matches!(report, RingFeedback::Presented { .. });
        if !aggregate {
            record(
                trace,
                json!({
                    "kind":"ring_feedback", "at_us":observed_at_us, "details":report,
                    "requested_frame":self.pending.map(|pending| pending.frame),
                    "validation_error":validation_error,
                    "boundary":WRITE_BOUNDARY,
                }),
            )?;
        }
        if validation_error.is_some() {
            // Preserve the original unacknowledged request; never convert a bad
            // receipt into success even if the worker said it wrote something.
            self.confirmed = None;
            return Ok(());
        }
        match report {
            RingFeedback::Presented {
                write_finished_at_us,
                requested_at_us,
                ..
            } => {
                let pending =
                    matched.ok_or_else(|| io::Error::other("ring trace receipt has no request"))?;
                if pending.aggregated {
                    let batch = self
                        .renewals
                        .as_mut()
                        .ok_or_else(|| io::Error::other("missing ring renewal batch"))?;
                    let latency = write_finished_at_us
                        .checked_sub(requested_at_us)
                        .ok_or_else(|| io::Error::other("ring trace write predates request"))?;
                    batch.presentations = batch.presentations.checked_add(1).ok_or_else(|| {
                        io::Error::other("ring renewal receipt counter exhausted")
                    })?;
                    batch.latency_min_us =
                        Some(batch.latency_min_us.map_or(latency, |old| old.min(latency)));
                    batch.latency_max_us =
                        Some(batch.latency_max_us.map_or(latency, |old| old.max(latency)));
                    batch.latency_total_us = batch
                        .latency_total_us
                        .checked_add(latency)
                        .ok_or_else(|| io::Error::other("ring renewal latency sum exhausted"))?;
                    batch.first_presented.get_or_insert(report);
                    batch.last_presented = Some(report);
                    batch.updated_at_us = observed_at_us;
                    batch.update(trace)?;
                }
                self.confirmed = Some(pending.frame);
                self.pending = None;
            }
            RingFeedback::Rejected { .. } => {
                let pending = matched
                    .ok_or_else(|| io::Error::other("ring trace rejection has no request"))?;
                if pending.aggregated {
                    let batch = self
                        .renewals
                        .as_mut()
                        .ok_or_else(|| io::Error::other("missing ring renewal batch"))?;
                    batch.rejections = batch.rejections.checked_add(1).ok_or_else(|| {
                        io::Error::other("ring renewal rejection counter exhausted")
                    })?;
                    batch.updated_at_us = observed_at_us;
                    batch.update(trace)?;
                }
                self.pending = None;
                self.confirmed = None;
                self.anchor = None;
            }
            RingFeedback::Blanked { .. } => {
                // A black-write receipt never acknowledges an in-flight frame.
                // Even if a late write receipt follows, the next request is raw.
                self.confirmed = None;
                self.anchor = None;
            }
        }
        Ok(())
    }
}

fn request_event(frame: RingFrame, role: &'static str) -> Value {
    json!({
        "kind":"ring_requested", "at_us":frame.requested_at_us,
        "owner":frame.permit.owner(), "phase":frame.phase, "ceiling":frame.ceiling,
        "permit_expires_at_us":frame.permit.expires_at().as_micros(),
        "playback_sequence":frame.snapshot.playback().map(|token| token.sequence()),
        "frame":frame, "trace_role":role,
    })
}

fn same_cue(a: RingFrame, b: RingFrame) -> bool {
    if a.permit.owner() != b.permit.owner()
        || a.phase != b.phase
        || a.ceiling != b.ceiling
        || a.snapshot.microphone_generation() != b.snapshot.microphone_generation()
        || a.snapshot.playback() != b.snapshot.playback()
    {
        return false;
    }
    // A waiting -> playback -> waiting cycle can retain the same enum and no
    // playback token while changing presentation lineage. Reuse the established
    // check solely for retention: uncertainty/expiry means keep a raw event.
    // This temporary guard grants no output; the real writer still checks b.
    let now = MonoTime::from_micros(b.requested_at_us);
    let mut guard = BoundaryGuard::new(b.snapshot.boot(), now);
    guard
        .install(now, b.snapshot)
        .and_then(|()| guard.check(now, a.permit, OutputKind::Light))
        .is_ok()
}

impl Renewals {
    fn event(&self) -> Value {
        json!({
            "kind":"ring_renewals", "at_us":self.first.requested_at_us,
            "last_updated_at_us":self.updated_at_us,
            "anchor_requested_at_us":self.anchor_requested_at_us,
            "owner":self.first.permit.owner(), "phase":self.first.phase,
            "requested":self.requests, "validated_presentations":self.presentations,
            "validated_rejections":self.rejections,
            "unacknowledged_requests":self.requests - self.presentations - self.rejections,
            "first_request":self.first, "last_request":self.last,
            "first_presented":self.first_presented, "last_presented":self.last_presented,
            "request_to_write_min_us":self.latency_min_us,
            "request_to_write_max_us":self.latency_max_us,
            "request_to_write_total_us":self.latency_total_us,
            "boundary":WRITE_BOUNDARY,
        })
    }

    fn update(&self, trace: &mut [Value]) -> Result<()> {
        let slot = trace
            .get_mut(self.index)
            .ok_or_else(|| io::Error::other("ring renewal trace slot was removed"))?;
        *slot = self.event();
        // Retain the exact last submitted request for existing evaluator phase
        // checks. This is a sample of a counted command, never another send or
        // proof of presentation. Indices do not move; its timestamp may advance
        // past unrelated intervening events, which retain their original slots.
        let tail = trace
            .get_mut(self.tail_index)
            .ok_or_else(|| io::Error::other("ring renewal tail slot was removed"))?;
        *tail = request_event(self.last, "renewal_tail");
        Ok(())
    }
}
