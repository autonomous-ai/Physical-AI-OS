//! Deterministic ownership for Lamp's live conversation.
//!
//! This crate neither decides whether speech addresses Lamp nor drives hardware.
//! The caller supplies admitted input and a shared monotonic clock. An async job
//! retains its original [`TurnOwner`] and planned [`OutputOwner`]; it must never relabel a late callback with
//! the currently visible owner. All queues and final writes retain that owner.
//!
//! [`BoundaryGuard::check`] belongs immediately before each bounded output write
//! in the sole actuator writer. Installing authority updates and writing output
//! must be serialized there. This is an ownership check, not a motion-safety
//! check, an acoustic interruption measurement, or a security/authentication
//! protocol. The transport must authenticate the controller, prioritize
//! revocation, and stop or flush output on failure.
#![forbid(unsafe_code)]

mod controller;
mod guard;
mod observation;
mod types;

pub use controller::Controller;
pub use guard::{BoundaryGuard, ZeroAuthority};
pub use observation::{Confidence, Freshness, Observation, ObservationTracker};
pub use types::{
    AdmissionState, AdmittedInput, BootId, CameraGrant, CaptureState, Error,
    MAX_AUTHORITY_LEASE_US, MAX_INPUT_LEASE_US, MAX_OUTPUT_LEASE_US, MonoTime, OutputKind,
    OutputOwner, OutputPermit, Permission, PlaybackToken, Snapshot, TurnOwner,
};
