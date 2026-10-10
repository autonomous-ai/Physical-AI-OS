//! Microphone privacy preflight. Status queries and prompts never open a stream.
use crate::{Result, files::Attempt};
use serde::Serialize;
use serde_json::{Value, json};
use std::{io, path::Path};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Access {
    NotDetermined,
    Restricted,
    Denied,
    Authorized,
    Unknown,
}

impl Access {
    pub fn from_raw(raw: i64) -> Self {
        match raw {
            0 => Self::NotDetermined,
            1 => Self::Restricted,
            2 => Self::Denied,
            3 => Self::Authorized,
            _ => Self::Unknown,
        }
    }

    pub fn capture_error(self, usage_description_present: bool) -> Option<&'static str> {
        match self {
            Self::NotDetermined => Some(
                "Microphone permission is not determined. Launch the observer app's authorize command and answer the native macOS prompt before recording.",
            ),
            Self::Restricted => Some(
                "Microphone access is restricted by macOS policy. A new prompt cannot remove that restriction.",
            ),
            Self::Denied => Some(
                "Microphone permission is denied. Enable this app in System Settings > Privacy & Security > Microphone, then relaunch it.",
            ),
            Self::Unknown => Some(
                "macOS returned an unknown microphone authorization status; capture is blocked.",
            ),
            Self::Authorized if !usage_description_present => Some(
                "NSMicrophoneUsageDescription is missing or empty in the main bundle. Run the packaged Lamp Room Observer app.",
            ),
            Self::Authorized => None,
        }
    }
}

pub fn snapshot() -> Result<Value> {
    #[cfg(target_os = "macos")]
    {
        native::snapshot()
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "microphone authorization requires macOS",
        )
        .into())
    }
}

/// Persist diagnostics for LaunchServices launches, whose stdout may be invisible.
pub fn probe(request: bool, output: Option<&Path>) -> Result<Value> {
    if request && output.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "authorize requires --out NEW_DIR for its permission result",
        )
        .into());
    }
    let attempt = output
        .map(|path| {
            Attempt::create_diagnostic(
                path,
                &json!({"operation": if request { "authorize" } else { "authorization" }}),
            )
        })
        .transpose()?;
    #[cfg(target_os = "macos")]
    let outcome = if request {
        native::request()
    } else {
        native::snapshot()
    };
    #[cfg(not(target_os = "macos"))]
    let outcome = snapshot();
    let mut report = match outcome {
        Ok(value) => value,
        Err(error) => {
            json!({"authorized": false, "capture_allowed": false, "error": error.to_string()})
        }
    };
    report["schema_version"] = json!(1);
    report["scope"] = json!("microphone_authorization_only_no_audio_captured");
    report["valid"] = json!(false); // Authorization is never a valid acoustic recording.
    report["completed_host_monotonic_ns"] = json!(lamp_ipc::monotonic_ns());
    if let Some(attempt) = attempt {
        report["attempt_id"] = json!(attempt.id);
        attempt.finish(&report)?;
    }
    Ok(report)
}

#[cfg(target_os = "macos")]
mod native {
    use super::*;
    use block2::RcBlock;
    use objc2::runtime::Bool;
    use objc2_av_foundation::{AVCaptureDevice, AVMediaType, AVMediaTypeAudio};
    use objc2_foundation::{NSBundle, NSString, ns_string};
    use std::{sync::mpsc, time::Duration};

    #[allow(unsafe_code)]
    fn audio_media_type() -> Result<&'static AVMediaType> {
        // SAFETY: AVFoundation exports an immutable, process-lifetime NSString.
        // The binding marks availability as optional; absence is an explicit error.
        unsafe { AVMediaTypeAudio }
            .ok_or_else(|| io::Error::other("AVMediaTypeAudio unavailable").into())
    }

    #[allow(unsafe_code)]
    fn read_access() -> Result<(Access, i64)> {
        let media = audio_media_type()?;
        // SAFETY: The documented class method accepts AVMediaTypeAudio, has no
        // hardware/device lifetime requirement and does not start capture.
        let raw = unsafe { AVCaptureDevice::authorizationStatusForMediaType(media) }.0 as i64;
        Ok((Access::from_raw(raw), raw))
    }

    pub fn snapshot() -> Result<Value> {
        let (access, raw) = read_access()?;
        let bundle = NSBundle::mainBundle();
        let description = bundle
            .objectForInfoDictionaryKey(ns_string!("NSMicrophoneUsageDescription"))
            .and_then(|value| value.downcast_ref::<NSString>().map(ToString::to_string));
        let usage_present = description
            .as_ref()
            .is_some_and(|text| !text.trim().is_empty());
        let reason = access.capture_error(usage_present);
        Ok(json!({
            "authorization_status": access, "authorization_raw": raw,
            "authorized": access == Access::Authorized, "capture_allowed": reason.is_none(),
            "preflight_error": reason,
            "usage_description_present": usage_present, "usage_description": description,
            "bundle_identifier": bundle.bundleIdentifier().map(|id| id.to_string()),
            "bundle_path": bundle.bundlePath().to_string(),
            "executable": std::env::current_exe()?.display().to_string(),
            "queried_host_monotonic_ns": lamp_ipc::monotonic_ns(),
            "note": "This reports the calling process's AVFoundation audio authorization. It does not prove audibility or identify TCC's responsible parent application."
        }))
    }

    #[allow(unsafe_code)]
    fn request_native(sender: mpsc::SyncSender<bool>) -> Result<()> {
        let media = audio_media_type()?;
        let completion = RcBlock::new(move |granted: Bool| {
            // One owned, thread-safe, bounded result; safe even after timeout.
            let _ = sender.try_send(granted.as_bool());
        });
        // SAFETY: A valid framework audio media type is supplied. AVFoundation
        // copies the escaping block; its closure owns only a SyncSender and no
        // borrowed data. It may run on an arbitrary dispatch queue. The caller
        // checked a nonempty usage description before reaching this function.
        unsafe {
            AVCaptureDevice::requestAccessForMediaType_completionHandler(media, &completion);
        }
        Ok(())
    }

    pub fn request() -> Result<Value> {
        let before = snapshot()?;
        let raw = before["authorization_raw"].as_i64().unwrap_or(-1);
        let mut request_sent = false;
        let mut timed_out = false;
        let mut completion_granted = None;
        let mut request_error = None;
        if Access::from_raw(raw) == Access::NotDetermined {
            if before["usage_description_present"] != true {
                request_error = Some("Native prompt not requested: main bundle lacks NSMicrophoneUsageDescription. Build and launch the observer app bundle.".to_owned());
            } else {
                let (sender, receiver) = mpsc::sync_channel(1);
                request_native(sender)?;
                request_sent = true;
                match receiver.recv_timeout(Duration::from_secs(60)) {
                    Ok(granted) => completion_granted = Some(granted),
                    Err(mpsc::RecvTimeoutError::Timeout) => timed_out = true,
                    Err(error) => request_error = Some(error.to_string()),
                }
            }
        }
        let mut after = snapshot()?;
        after["before"] = before;
        after["request_sent"] = json!(request_sent);
        after["completion_granted"] = json!(completion_granted);
        after["request_timed_out"] = json!(timed_out);
        after["request_error"] = json!(request_error);
        after["request_timeout_seconds"] = json!(60);
        // A timeout is not a user's answer, even if a racing status changed.
        if timed_out || request_error.is_some() {
            after["capture_allowed"] = json!(false);
        }
        Ok(after)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_authorized_with_usage_description_can_capture() {
        for raw in [-1, 0, 1, 2, 4, i64::MAX] {
            assert!(Access::from_raw(raw).capture_error(true).is_some());
        }
        assert!(Access::Authorized.capture_error(false).is_some());
        assert!(Access::Authorized.capture_error(true).is_none());
    }

    #[test]
    fn denied_and_not_determined_have_different_remedies() {
        assert!(
            Access::Denied
                .capture_error(true)
                .unwrap()
                .contains("System Settings")
        );
        assert!(
            Access::NotDetermined
                .capture_error(true)
                .unwrap()
                .contains("native macOS prompt")
        );
        assert!(
            Access::Restricted
                .capture_error(true)
                .unwrap()
                .contains("restricted")
        );
    }

    #[test]
    fn permission_request_requires_report_before_any_native_call() {
        assert!(probe(true, None).is_err());
    }
}
