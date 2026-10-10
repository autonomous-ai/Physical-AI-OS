//! Continuous room-audio evidence. Callback timestamps are not acoustic boundaries.
pub mod authorization;
pub mod capture;
mod files;
#[cfg(target_os = "macos")]
mod macos;
pub mod playback;

use serde::Serialize;
use serde_json::{Value, json};
use std::{io, path::PathBuf};
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const CPAL_REVISION: &str = "79275c2313da30e9f8b1ad8168385d091a2c9ada";

#[derive(Clone, Debug, Serialize)]
pub struct RecordOptions {
    pub input: String,
    pub seconds: u16,
    pub output: PathBuf,
}

impl RecordOptions {
    pub fn validate(&self) -> Result<()> {
        if self.input.trim().is_empty()
            || self.input.len() > 256
            || self.input.contains('\0')
            || !(1..=600).contains(&self.seconds)
            || self.output.as_os_str().is_empty()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "require an exact input name, duration 1..600 seconds and a fresh output directory",
            )
            .into());
        }
        Ok(())
    }
}

/// Reject ambiguous names instead of silently selecting another microphone.
pub fn unique_name_index(names: &[String], requested: &str) -> Result<usize> {
    let matches: Vec<_> = names
        .iter()
        .enumerate()
        .filter(|(_, name)| *name == requested)
        .collect();
    if matches.len() != 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "input name must match exactly one available input device; run devices",
        )
        .into());
    }
    Ok(matches[0].0)
}

pub fn devices() -> Result<Value> {
    #[cfg(target_os = "macos")]
    {
        macos::devices()
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "room observer currently supports macOS only",
        )
        .into())
    }
}

pub fn record(options: &RecordOptions) -> Result<Value> {
    options.validate()?;
    let attempt = files::Attempt::create(options)?;
    #[cfg(target_os = "macos")]
    let outcome = macos::record(options, &attempt);
    #[cfg(not(target_os = "macos"))]
    let outcome: Result<Value> = Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "room observer currently supports macOS only",
    )
    .into());
    let mut report = match outcome {
        Ok(value) => value,
        Err(error) => json!({"valid": false, "status": "failed", "error": error.to_string()}),
    };
    report["schema_version"] = json!(1);
    report["attempt_id"] = json!(attempt.id);
    report["requested"] = serde_json::to_value(options)?;
    report["cpal_git_revision"] = json!(CPAL_REVISION);
    report["clock_domains"] = json!({
        "host_monotonic_ns": "lamp-ipc CLOCK_MONOTONIC; same-host events only",
        "capture_stream_ns": "CPAL CoreAudio mach_absolute_time converted to ns",
        "callback_stream_ns": "CPAL CoreAudio stream clock; do not equate with host CLOCK_MONOTONIC",
        "wav": "sample frame index divided by recorded sample rate; invalid after any discontinuity",
        "acoustic_boundaries": "not inferred; annotate continuous room audio separately"
    });
    report["scope"] = json!("continuous_observer_capture_integrity_only_not_conversation_score");
    report["completed_host_monotonic_ns"] = json!(lamp_ipc::monotonic_ns());
    attempt.finish(&report)?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_duplicate_and_unknown_devices() {
        let names = vec!["Mic".into(), "Mic".into(), "External".into()];
        assert!(unique_name_index(&names, "Mic").is_err());
        assert!(unique_name_index(&names, "missing").is_err());
        assert_eq!(unique_name_index(&names, "External").unwrap(), 2);
    }
    #[test]
    fn validates_before_any_device_access() {
        for seconds in [0, 601] {
            assert!(
                RecordOptions {
                    input: "Mic".into(),
                    seconds,
                    output: "new".into()
                }
                .validate()
                .is_err()
            );
        }
        assert!(
            RecordOptions {
                input: " ".into(),
                seconds: 1,
                output: "new".into()
            }
            .validate()
            .is_err()
        );
    }
}
