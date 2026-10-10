//! Pure camera lifecycle and bounded latest-frame ownership.
//!
//! The Linux backend is opt-in and opens only its explicit provisioned camera
//! at a permitted start. No inference, decoding, provider I/O or motion control
//! is implemented here. A supervised worker must service its
//! trusted priority control path before every operation and at least every 2 ms.
//! Port implementations must be nonblocking and honor the supplied budget. This
//! library can detect an overrun only after a call returns; it cannot preempt a
//! blocking implementation or kernel call. Process supervision remains required.
//!
//! A frame retains its original camera privacy grant. Host dequeue age bounds
//! local retention, not scene/exposure age. Delivery is a nonblocking handoff:
//! never wait, infer, or perform network I/O while borrowing frame bytes. Any
//! downstream copy must retain and revalidate the original grant and frame ID.
//! Privacy cannot retract bytes already exported to another process or provider.

mod capture;
mod types;

pub use capture::Capture;
pub use types::*;

#[cfg(any(target_os = "linux", test))]
mod backend;
#[cfg(target_os = "linux")]
pub mod linux;
