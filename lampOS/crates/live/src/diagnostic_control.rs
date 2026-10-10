//! Diagnostic receipts and final certification, separate from audio authority.
use crate::diagnostics::{FinishOutcome, FinishReport, StreamKind};
use lamp_interaction::BootId;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};

const MAX_MARKER_BYTES: u64 = 16 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticStream {
    Capture,
    Render,
}
impl From<StreamKind> for DiagnosticStream {
    fn from(stream: StreamKind) -> Self {
        match stream {
            StreamKind::Capture => Self::Capture,
            StreamKind::Render => Self::Render,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticStarted {
    /// Shared controller/session boot; transport separately checks worker boot.
    pub boot: BootId,
    pub stream: DiagnosticStream,
    pub started_at_us: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticOutcome {
    Complete,
    Invalid,
    TimedOut,
    WriterDisconnected,
    InvalidWait,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticFinished {
    pub started: DiagnosticStarted,
    pub valid: bool,
    /// True only when Recorder::finish received the writer's acknowledgement.
    pub acknowledged: bool,
    pub outcome: DiagnosticOutcome,
    pub faults: u64,
    pub completion_marker_published: bool,
    pub completion_sha256: Option<String>,
    pub finalized_at_us: Option<u64>,
    pub records_written: Option<u64>,
    pub records_rejected: Option<u64>,
    pub capture_frames: Option<u64>,
    pub render_frames: Option<u64>,
    pub accepted_zero_frames: Option<u64>,
}
impl DiagnosticFinished {
    pub fn from_report(started: DiagnosticStarted, report: FinishReport) -> Self {
        let summary = report.summary.as_ref();
        Self {
            started,
            valid: report.valid,
            acknowledged: summary.is_some(),
            outcome: match report.outcome {
                FinishOutcome::Complete => DiagnosticOutcome::Complete,
                FinishOutcome::Invalid => DiagnosticOutcome::Invalid,
                FinishOutcome::TimedOut => DiagnosticOutcome::TimedOut,
                FinishOutcome::WriterDisconnected => DiagnosticOutcome::WriterDisconnected,
                FinishOutcome::InvalidWait => DiagnosticOutcome::InvalidWait,
            },
            faults: report.faults,
            completion_marker_published: report.completion_marker_published,
            completion_sha256: report.completion_sha256,
            finalized_at_us: summary.map(|s| s.finalized_at_us),
            records_written: summary.map(|s| s.records_written),
            records_rejected: summary.map(|s| s.records_rejected),
            capture_frames: summary.map(|s| s.capture_frames),
            render_frames: summary.map(|s| s.render_frames),
            accepted_zero_frames: summary.map(|s| s.accepted_zero_frames),
        }
    }
}

/// Control path only, after all hardware workers have stopped. A marker alone
/// is never sufficient, including a marker published after a finish timeout.
pub fn certify(
    directory: &Path,
    expected: DiagnosticStarted,
    received: Option<&DiagnosticFinished>,
) -> io::Result<()> {
    let receipt =
        received.ok_or_else(|| io::Error::other("missing diagnostic finish acknowledgement"))?;
    if receipt.started != expected
        || !receipt.valid
        || !receipt.acknowledged
        || receipt.outcome != DiagnosticOutcome::Complete
        || receipt.faults != 0
        || !receipt.completion_marker_published
        || receipt
            .finalized_at_us
            .is_none_or(|at| at < expected.started_at_us)
    {
        return Err(io::Error::other(
            "diagnostic finish was not valid and acknowledged",
        ));
    }
    let hash = receipt
        .completion_sha256
        .as_deref()
        .filter(|hash| hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| io::Error::other("diagnostic completion hash missing or invalid"))?;
    let directory = rustix::fs::open(
        directory,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?;
    let descriptor = rustix::fs::openat(
        &directory,
        "complete.json",
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?;
    let file = File::from(descriptor);
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > MAX_MARKER_BYTES {
        return Err(io::Error::other(
            "diagnostic marker is not a bounded regular file",
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_MARKER_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_MARKER_BYTES || format!("{:x}", Sha256::digest(&bytes)) != hash {
        return Err(io::Error::other("diagnostic marker hash mismatch"));
    }
    let marker: serde_json::Value = serde_json::from_slice(&bytes)?;
    if marker["boot"] != serde_json::to_value(expected.boot)?
        || marker["stream"] != serde_json::to_value(expected.stream)?
        || marker["started_at_us"].as_u64() != Some(expected.started_at_us)
        || marker["finalized_at_us"].as_u64() != receipt.finalized_at_us
        || marker["valid"].as_bool() != Some(true)
        || marker["faults"].as_u64() != Some(0)
        || marker["records_rejected"].as_u64() != Some(0)
    {
        return Err(io::Error::other(
            "diagnostic marker identity or validity mismatch",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        diagnostics::{
            Config, DEFAULT_FINISH_WAIT, EndMeta, EndReason, PrivacyMeta, Recorder, RenderMeta,
            SilenceKind,
        },
        process::SessionDirectory,
    };

    fn completed(directory: &Path) -> (DiagnosticStarted, DiagnosticFinished) {
        let boot = BootId::new([8; 16]).unwrap();
        let mut recorder =
            Recorder::start(Config::new(directory.to_owned(), boot, StreamKind::Render)).unwrap();
        let start = DiagnosticStarted {
            boot,
            stream: DiagnosticStream::Render,
            started_at_us: recorder.started_at_us(),
        };
        let at = lamp_ipc::monotonic_us();
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 1,
            open: true,
        });
        recorder.try_silence(
            RenderMeta {
                playback_epoch: 1,
                privacy_generation: 1,
                first_sample: 0,
                end_sample: 240,
                accepted_at_us: at,
                queue_observed_at_us: at,
                queued_frames: 240,
                owner: None,
                chunk_sequence: None,
            },
            SilenceKind::Prime,
            240,
        );
        let report = recorder.finish(
            EndMeta {
                at_us: lamp_ipc::monotonic_us(),
                reason: EndReason::Stopped,
            },
            DEFAULT_FINISH_WAIT,
        );
        (start, DiagnosticFinished::from_report(start, report))
    }

    #[test]
    fn marker_and_matching_acknowledgement_are_both_required() {
        let root = SessionDirectory::create().unwrap();
        let leaf = root.path.join("diagnostic");
        let (start, mut receipt) = completed(&leaf);
        certify(&leaf, start, Some(&receipt)).unwrap();
        assert!(certify(&leaf, start, None).is_err());
        receipt.outcome = DiagnosticOutcome::TimedOut;
        receipt.valid = false;
        receipt.acknowledged = false;
        // Even a complete, correctly hashed marker cannot undo a timed-out finish.
        assert!(certify(&leaf, start, Some(&receipt)).is_err());
        std::fs::remove_dir_all(leaf).unwrap();
    }

    #[test]
    fn stale_identity_or_replaced_marker_cannot_certify_a_run() {
        let root = SessionDirectory::create().unwrap();
        let leaf = root.path.join("diagnostic");
        let (start, receipt) = completed(&leaf);
        let mut wrong = start;
        wrong.started_at_us += 1;
        assert!(certify(&leaf, wrong, Some(&receipt)).is_err());
        std::fs::write(leaf.join("complete.json"), b"{}").unwrap();
        assert!(certify(&leaf, start, Some(&receipt)).is_err());
        std::fs::remove_dir_all(leaf).unwrap();
    }
}
