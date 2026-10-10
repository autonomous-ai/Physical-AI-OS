//! Runtime integration for a directed conversation qualification session.
//!
//! Speech activity is an observation, not evidence of who is being addressed.
//! The initial harness requires an explicitly opened, finite directed session;
//! this does not qualify open-desk addressee recognition or background restraint.
pub mod activity;
pub mod admission;
pub mod camera_config;
pub mod camera_inspect;
pub mod camera_worker;
pub mod choreography;
pub mod config;
pub mod diagnostic_control;
pub mod diagnostics;
pub mod fixture_provider;
pub mod options;
#[cfg(target_os = "linux")]
pub mod physical_privacy;
#[cfg(any(target_os = "linux", test))]
mod playback;
pub mod privacy;
pub mod process;
pub(crate) mod provider_flow;
pub mod provider_worker;
pub mod reference;
pub mod ring_wire;
pub mod ring_worker;
pub mod transport;
pub mod wire;

#[cfg(target_os = "linux")]
pub mod audio_workers;
pub mod coordinator;
