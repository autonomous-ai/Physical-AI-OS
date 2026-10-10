//! Physical execution: a Lamp-side relay (`lamp-session`) and a Mac-side
//! backend that plays cached stimuli through the explicit iMac speakers.
//!
//! The relay binds lamp-live's existing optional cue socket and forwards cues,
//! runtime stdout and the final trace as JSON lines over whatever transport the
//! operator supplies (normally an existing SSH command). The Mac backend never
//! connects to a device on its own initiative and never edits mixers, services
//! or motor state; exclusive hardware ownership and restoration remain operator
//! duties, as documented for `lamp-live directed`.
pub mod fake_lamp;
pub mod mac;
pub mod protocol;
pub mod session;
