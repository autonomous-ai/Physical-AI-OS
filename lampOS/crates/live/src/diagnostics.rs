//! Opt-in local PCM evidence, independent of audio authority and cloud transport.
//! A durable completion marker AND a successful finish acknowledgement are required.
use crate::options::SoftwareProcessing;
use lamp_interaction::{BootId, TurnOwner};
use rtrb::{Consumer, Producer, RingBuffer};
use serde::Serialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{self, BufWriter, Write},
    os::{
        fd::OwnedFd,
        unix::fs::{MetadataExt, PermissionsExt},
    },
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

pub const QUEUE_RECORDS: usize = 64;
pub const MAX_SECONDS: u16 = 600;
pub const MAX_BYTES: u64 = 160 * 1024 * 1024;
pub const DEFAULT_FINISH_WAIT: Duration = Duration::from_millis(500);
pub const MAX_FINISH_WAIT: Duration = Duration::from_secs(1);
const FINAL_RESERVE: u64 = 8192;
const MAX_LINE_BYTES: usize = 4096;
const OVERFLOW: u64 = 1;
const WRITER_ERROR: u64 = 2;
const SEQUENCE_GAP: u64 = 4;
const CURSOR_GAP: u64 = 8;
const BOUNDARY_ERROR: u64 = 16;
const BYTE_LIMIT: u64 = 32;
const DURATION_LIMIT: u64 = 64;
const INVALID_RECORD: u64 = 128;
const ABANDONED: u64 = 256;
const FINISH_TIMEOUT: u64 = 512;
const PATH_CHANGED: u64 = 1024;
const AUDIO_FAULT: u64 = 2048;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StreamKind {
    Capture,
    Render,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub directory: PathBuf,
    pub boot: BootId,
    pub stream: StreamKind,
    /// Actual capture processing when supplied by the worker. None is unknown
    /// (or not applicable for render), never an inferred default mode.
    pub software_processing: Option<SoftwareProcessing>,
    pub max_seconds: u16,
    pub max_bytes: u64,
}
impl Config {
    pub fn new(directory: PathBuf, boot: BootId, stream: StreamKind) -> Self {
        Self {
            directory,
            boot,
            stream,
            software_processing: None,
            max_seconds: MAX_SECONDS,
            max_bytes: MAX_BYTES,
        }
    }
}

/// Host/driver and DSP accounting only; none of these is an acoustic timestamp.
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct ReferenceMeta {
    pub playback_epoch: u64,
    pub accepted_through: u64,
    pub analysed_through: u64,
    pub capture_blocks_processed: u64,
    pub capture_blocks_before_clock_start: u64,
    pub accepted_at_us: u64,
    pub analysed_at_us: u64,
    pub queue_observed_at_us: u64,
    pub queued_frames: u64,
    pub clock_start_requested_at_us: Option<u64>,
    pub clock_started_at_us: Option<u64>,
    pub ignored_old_epoch_packets: u64,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct CaptureMeta {
    pub epoch: u64,
    pub dsp_epoch: u64,
    pub privacy_generation: u64,
    pub frame_sequence: u64,
    pub first_read_started_at_us: u64,
    pub last_read_started_at_us: u64,
    pub read_completed_at_us: u64,
    pub successful_reads: u16,
    pub alsa_status_monotonic_us: Option<u64>,
    pub status_observed_at_us: u64,
    pub available_frames: i64,
    pub delayed_frames: i64,
    pub processing_completed_at_us: u64,
    pub aec_queue_delay_ms: u16,
    /// Cached internal AEC buffer alignment, not a converged acoustic estimate.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub aec_internal_alignment_ms: Option<i32>,
    pub reference: ReferenceMeta,
    pub vad_score: f32,
}

/// Actual ALSA acceptance; accepted PCM may subsequently be discarded unplayed.
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct RenderMeta {
    pub playback_epoch: u64,
    pub privacy_generation: u64,
    pub first_sample: u64,
    pub end_sample: u64,
    pub accepted_at_us: u64,
    pub queue_observed_at_us: u64,
    pub queued_frames: i64,
    pub owner: Option<TurnOwner>,
    pub chunk_sequence: Option<u64>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SilenceKind {
    Prime,
    Idle,
    SpeechGap,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResetReason {
    Start,
    CaptureRestart,
    DspReset,
    PlaybackReset,
    PrivacyChange,
    Fault,
    Stop,
}
#[derive(Clone, Copy, Debug, Serialize)]
pub struct DiscardedRange {
    pub playback_epoch: u64,
    pub retired_through: u64,
    pub accepted_through: u64,
}
#[derive(Clone, Copy, Debug, Serialize)]
pub struct ResetMeta {
    pub at_us: u64,
    pub reason: ResetReason,
    pub capture_epoch: u64,
    pub dsp_epoch: u64,
    pub playback_epoch: u64,
    pub privacy_generation: u64,
    pub discarded: Option<DiscardedRange>,
}
/// An observed stream-privacy boundary, never permission to read or play PCM.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct PrivacyMeta {
    pub at_us: u64,
    pub generation: u64,
    pub open: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EndReason {
    Completed,
    Stopped,
    PrivacyClosed,
    Fault,
}
#[derive(Clone, Copy, Debug, Serialize)]
pub struct EndMeta {
    pub at_us: u64,
    pub reason: EndReason,
}

// Fixed inline storage avoids allocation/indirection in the audio producer.
// The whole 64-slot queue is bounded; boxing the larger PCM variant is unsuitable.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Copy)]
enum Record {
    Capture(CaptureMeta, [i16; 160], [i16; 160]),
    Render(RenderMeta, u16, [i16; 240]),
    Silence(RenderMeta, SilenceKind, u16),
    Reset(ResetMeta),
    Privacy(PrivacyMeta),
    End(EndMeta),
}
impl Record {
    fn at_us(&self) -> u64 {
        match self {
            Self::Capture(m, ..) => m.processing_completed_at_us,
            Self::Render(m, ..) | Self::Silence(m, ..) => m.queue_observed_at_us,
            Self::Reset(m) => m.at_us,
            Self::Privacy(m) => m.at_us,
            Self::End(m) => m.at_us,
        }
    }
}
#[derive(Clone, Copy)]
struct Envelope {
    sequence: u64,
    record: Record,
}
#[derive(Default)]
struct Shared {
    faults: AtomicU64,
    queued: AtomicU64,
    rejected: AtomicU64,
    closed: AtomicBool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubmitResult {
    Queued,
    Disabled,
    InvalidRecord,
    Full,
    DurationLimit,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FinishOutcome {
    Complete,
    Invalid,
    TimedOut,
    WriterDisconnected,
    InvalidWait,
}
#[derive(Clone, Debug, Serialize)]
pub struct Summary {
    pub valid: bool,
    pub boot: BootId,
    pub stream: StreamKind,
    pub software_processing: Option<SoftwareProcessing>,
    pub started_at_us: u64,
    pub finalized_at_us: u64,
    pub faults: u64,
    pub records_queued: u64,
    pub records_written: u64,
    pub records_rejected: u64,
    pub capture_frames: u64,
    pub render_frames: u64,
    pub accepted_zero_frames: u64,
    pub boundaries: u64,
    pub bytes_reserved: u64,
    pub pre_aec_sha256: String,
    pub post_aec_sha256: String,
    pub render_accepted_sha256: String,
    pub events_sha256: String,
    pub end: Option<EndMeta>,
    pub writer_error: Option<String>,
}
#[derive(Clone, Debug, Serialize)]
pub struct FinishReport {
    pub valid: bool,
    pub outcome: FinishOutcome,
    pub faults: u64,
    pub completion_marker_published: bool,
    pub completion_sha256: Option<String>,
    pub summary: Option<Summary>,
}

struct Acknowledgement {
    summary: Summary,
    marker_published: bool,
    manifest_sha256: Option<String>,
}

/// One producer only. Construct before activation, retain only while opted in.
/// Drop is nonblocking and invalidates an unfinished recording.
pub struct Recorder {
    producer: Option<Producer<Envelope>>,
    shared: Arc<Shared>,
    acknowledgement: mpsc::Receiver<Acknowledgement>,
    worker: Option<thread::JoinHandle<()>>,
    config: Config,
    started_at_us: u64,
    next_sequence: u64,
    shutdown_requested: bool,
}
impl Recorder {
    pub fn start(config: Config) -> io::Result<Self> {
        Self::start_inner(config, Hooks::default())
    }
    fn start_inner(config: Config, hooks: Hooks) -> io::Result<Self> {
        if config.stream == StreamKind::Render && config.software_processing.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "capture processing provenance cannot describe a render stream",
            ));
        }
        if !(1..=MAX_SECONDS).contains(&config.max_seconds)
            || !(2 * FINAL_RESERVE..=MAX_BYTES).contains(&config.max_bytes)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "diagnostic limits require 1..600 seconds and 16 KiB..160 MiB",
            ));
        }
        let started_at_us = lamp_ipc::monotonic_us();
        let storage = Storage::create(&config, started_at_us)?;
        let shared = Arc::new(Shared::default());
        let (mut producer, mut consumer) = RingBuffer::new(QUEUE_RECORDS);
        // Pre-touch every queue slot before the audio loop can submit records.
        let warm = Envelope {
            sequence: 0,
            record: Record::Capture(CaptureMeta::default(), [0; 160], [0; 160]),
        };
        for _ in 0..QUEUE_RECORDS {
            producer
                .push(warm)
                .map_err(|_| io::Error::other("queue initialization"))?;
        }
        for _ in 0..QUEUE_RECORDS {
            consumer
                .pop()
                .map_err(|_| io::Error::other("queue initialization"))?;
        }
        let (tx, acknowledgement) = mpsc::sync_channel(1);
        let worker_shared = shared.clone();
        let worker_config = config.clone();
        let worker = thread::Builder::new()
            .name("lamp-audio-diagnostics".into())
            .spawn(move || {
                let summary = run_writer(
                    consumer,
                    storage,
                    worker_shared,
                    worker_config,
                    started_at_us,
                    hooks,
                );
                let _ = tx.try_send(summary);
            })?;
        Ok(Self {
            producer: Some(producer),
            shared,
            acknowledgement,
            worker: Some(worker),
            config,
            started_at_us,
            next_sequence: 1,
            shutdown_requested: false,
        })
    }
    pub fn started_at_us(&self) -> u64 {
        self.started_at_us
    }
    pub fn faults(&self) -> u64 {
        self.shared.faults.load(Ordering::Acquire)
    }
    fn invalid(&self) -> SubmitResult {
        self.shared
            .faults
            .fetch_or(INVALID_RECORD, Ordering::Release);
        self.shared.rejected.fetch_add(1, Ordering::Relaxed);
        SubmitResult::InvalidRecord
    }
    fn submit(&mut self, record: Record) -> SubmitResult {
        if self.faults() != 0 || self.producer.is_none() {
            self.shared.rejected.fetch_add(1, Ordering::Relaxed);
            return SubmitResult::Disabled;
        }
        let elapsed = record.at_us().checked_sub(self.started_at_us);
        if elapsed.is_none() {
            return self.invalid();
        }
        if elapsed.unwrap_or(u64::MAX) > u64::from(self.config.max_seconds) * 1_000_000 {
            self.shared
                .faults
                .fetch_or(DURATION_LIMIT, Ordering::Release);
            self.shared.rejected.fetch_add(1, Ordering::Relaxed);
            return SubmitResult::DurationLimit;
        }
        let Some(next) = self.next_sequence.checked_add(1) else {
            return self.invalid();
        };
        let envelope = Envelope {
            sequence: self.next_sequence,
            record,
        };
        self.next_sequence = next;
        if self
            .producer
            .as_mut()
            .expect("checked producer")
            .push(envelope)
            .is_err()
        {
            self.shared.faults.fetch_or(OVERFLOW, Ordering::Release);
            self.shared.rejected.fetch_add(1, Ordering::Relaxed);
            return SubmitResult::Full;
        }
        self.shared.queued.fetch_add(1, Ordering::Release);
        SubmitResult::Queued
    }
    pub fn try_capture(
        &mut self,
        meta: CaptureMeta,
        pre_aec: &[i16; 160],
        post_aec: &[i16; 160],
    ) -> SubmitResult {
        if self.config.stream != StreamKind::Capture
            || meta.epoch == 0
            || meta.dsp_epoch == 0
            || !meta.vad_score.is_finite()
            || !(0.0..=1.0).contains(&meta.vad_score)
            || meta.successful_reads == 0
            || meta.first_read_started_at_us > meta.last_read_started_at_us
            || meta.last_read_started_at_us > meta.read_completed_at_us
            || meta.read_completed_at_us > meta.processing_completed_at_us
        {
            return self.invalid();
        }
        self.submit(Record::Capture(meta, *pre_aec, *post_aec))
    }
    pub fn try_render(&mut self, meta: RenderMeta, accepted: &[i16]) -> SubmitResult {
        if !self.render_valid(meta, accepted.len()) {
            return self.invalid();
        }
        let mut samples = [0; 240];
        samples[..accepted.len()].copy_from_slice(accepted);
        self.submit(Record::Render(meta, accepted.len() as u16, samples))
    }
    pub fn try_silence(
        &mut self,
        meta: RenderMeta,
        kind: SilenceKind,
        frames: u16,
    ) -> SubmitResult {
        if !self.render_valid(meta, usize::from(frames))
            || meta.owner.is_some()
            || meta.chunk_sequence.is_some()
        {
            return self.invalid();
        }
        self.submit(Record::Silence(meta, kind, frames))
    }
    fn render_valid(&self, meta: RenderMeta, frames: usize) -> bool {
        self.config.stream == StreamKind::Render
            && (1..=240).contains(&frames)
            && meta.playback_epoch > 0
            && meta.accepted_at_us <= meta.queue_observed_at_us
            && meta.first_sample.checked_add(frames as u64) == Some(meta.end_sample)
            && meta
                .owner
                .is_none_or(|owner| owner.boot() == self.config.boot)
    }
    pub fn try_reset(&mut self, meta: ResetMeta) -> SubmitResult {
        if meta
            .discarded
            .is_some_and(|d| d.retired_through > d.accepted_through)
        {
            return self.invalid();
        }
        self.submit(Record::Reset(meta))
    }
    pub fn try_privacy(&mut self, meta: PrivacyMeta) -> SubmitResult {
        self.submit(Record::Privacy(meta))
    }

    /// Control path only, after audio/privacy shutdown. Never call from an audio callback.
    /// Both this successful acknowledgement and complete.json are needed for validity.
    pub fn finish(mut self, end: EndMeta, timeout: Duration) -> FinishReport {
        self.shutdown_requested = true;
        if timeout.is_zero() || timeout > MAX_FINISH_WAIT {
            self.shared
                .faults
                .fetch_or(FINISH_TIMEOUT, Ordering::Release);
            self.close_producer();
            return self.finish_report(FinishOutcome::InvalidWait, None);
        }
        let _ = self.submit(Record::End(end));
        self.close_producer();
        match self.acknowledgement.recv_timeout(timeout) {
            Ok(ack) => {
                let outcome = if ack.summary.valid && ack.marker_published && self.faults() == 0 {
                    FinishOutcome::Complete
                } else {
                    FinishOutcome::Invalid
                };
                self.finish_report(outcome, Some(ack))
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.shared
                    .faults
                    .fetch_or(FINISH_TIMEOUT, Ordering::Release);
                self.finish_report(FinishOutcome::TimedOut, None)
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.shared.faults.fetch_or(WRITER_ERROR, Ordering::Release);
                self.finish_report(FinishOutcome::WriterDisconnected, None)
            }
        }
    }
    fn close_producer(&mut self) {
        self.producer.take();
        self.shared.closed.store(true, Ordering::Release);
        // Dropping the join handle detaches. A blocked filesystem call cannot
        // turn a bounded acknowledgement wait into an unbounded join.
        self.worker.take();
    }
    fn finish_report(&self, outcome: FinishOutcome, ack: Option<Acknowledgement>) -> FinishReport {
        FinishReport {
            valid: outcome == FinishOutcome::Complete,
            outcome,
            faults: self.faults(),
            completion_marker_published: ack.as_ref().is_some_and(|a| a.marker_published),
            completion_sha256: ack.as_ref().and_then(|a| a.manifest_sha256.clone()),
            summary: ack.map(|a| a.summary),
        }
    }
}
impl Drop for Recorder {
    fn drop(&mut self) {
        if !self.shutdown_requested {
            self.shared.faults.fetch_or(ABANDONED, Ordering::Release);
        }
        self.close_producer();
    }
}

struct TrackedFile {
    file: BufWriter<File>,
    hash: Sha256,
    bytes: u64,
    name: &'static str,
    dev: u64,
    ino: u64,
}
impl TrackedFile {
    fn new(directory: &OwnedFd, name: &'static str) -> io::Result<Self> {
        let file = File::from(rustix::fs::openat(
            directory,
            name,
            rustix::fs::OFlags::WRONLY
                | rustix::fs::OFlags::CREATE
                | rustix::fs::OFlags::EXCL
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        )?);
        let meta = file.metadata()?;
        if !meta.is_file() || meta.nlink() != 1 {
            return Err(io::Error::other(
                "diagnostic artifact is not an exclusive regular file",
            ));
        }
        Ok(Self {
            file: BufWriter::with_capacity(16 * 1024, file),
            hash: Sha256::new(),
            bytes: 0,
            name,
            dev: meta.dev(),
            ino: meta.ino(),
        })
    }
    fn write(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.file.write_all(bytes)?;
        self.hash.update(bytes);
        self.bytes += bytes.len() as u64;
        Ok(())
    }
    fn sync(&mut self) -> io::Result<()> {
        self.file.flush()?;
        self.file.get_ref().sync_all()
    }
    fn hash(&self) -> String {
        format!("{:x}", self.hash.clone().finalize())
    }
}
struct Storage {
    parent: OwnedFd,
    directory: OwnedFd,
    parent_path: PathBuf,
    leaf: std::ffi::OsString,
    parent_identity: (u64, u64),
    directory_identity: (u64, u64),
    pre: TrackedFile,
    post: TrackedFile,
    render: TrackedFile,
    events: TrackedFile,
    pending: TrackedFile,
    result: TrackedFile,
    used: u64,
    maximum: u64,
}
impl Storage {
    fn create(config: &Config, started_at_us: u64) -> io::Result<Self> {
        let parent_path = config
            .directory
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "require a fresh leaf under an existing private parent",
                )
            })?
            .to_path_buf();
        let leaf = config
            .directory
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing diagnostic leaf"))?
            .to_os_string();
        let parent = rustix::fs::open(
            &parent_path,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )?;
        let parent_stat = rustix::fs::fstat(&parent)?;
        if parent_stat.st_uid != rustix::process::geteuid().as_raw()
            || parent_stat.st_mode & 0o077 != 0
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "diagnostic parent must be owned by this user and private (0700)",
            ));
        }
        rustix::fs::mkdirat(&parent, &leaf, rustix::fs::Mode::RWXU)?;
        let directory = rustix::fs::openat(
            &parent,
            &leaf,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )?;
        let stat = rustix::fs::fstat(&directory)?;
        if stat.st_uid != rustix::process::geteuid().as_raw() || stat.st_mode & 0o077 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "diagnostic leaf is not private",
            ));
        }
        let pending = TrackedFile::new(&directory, "manifest.pending.json")?;
        let pre = TrackedFile::new(&directory, "pre_aec.pcm16le")?;
        let post = TrackedFile::new(&directory, "post_aec.pcm16le")?;
        let render = TrackedFile::new(&directory, "render_accepted.pcm16le")?;
        let events = TrackedFile::new(&directory, "events.jsonl")?;
        let result = TrackedFile::new(&directory, "result.json")?;
        let mut storage = Self {
            parent,
            directory,
            parent_path,
            leaf,
            parent_identity: (parent_stat.st_dev as u64, parent_stat.st_ino),
            directory_identity: (stat.st_dev as u64, stat.st_ino),
            pre,
            post,
            render,
            events,
            pending,
            result,
            used: 0,
            maximum: config.max_bytes,
        };
        let start = json!({"schema_version":1,"status":"incomplete","valid":false,"boot":config.boot,"stream":config.stream,
            "software_processing":config.software_processing,
            "started_at_us":started_at_us,"max_seconds":config.max_seconds,"max_bytes":config.max_bytes,"queue_records":QUEUE_RECORDS,"queue_record_bytes":std::mem::size_of::<Envelope>(),
            "pre_aec":"ALSA-resampled mono PCM16 little-endian 16000 Hz; not native raw microphone",
            "post_aec":"AEC output mono PCM16 little-endian 16000 Hz",
            "render_accepted":"actual ALSA-accepted mono PCM16 little-endian 24000 Hz; not proof of played frames",
            "clock":"local host CLOCK_MONOTONIC microseconds with boot; driver status clocks separately labelled",
            "acceptance":"complete.json AND successful bounded finish acknowledgement required; timeout invalidates even later files"});
        let bytes = json_bytes(&start)?;
        storage.reserve(bytes.len() as u64, false)?;
        storage.pending.write(&bytes)?;
        storage.pending.sync()?;
        storage.verify_identity()?;
        rustix::fs::fsync(&storage.directory)?;
        Ok(storage)
    }
    fn reserve(&mut self, bytes: u64, finalizing: bool) -> io::Result<()> {
        let ceiling = if finalizing {
            self.maximum
        } else {
            self.maximum - FINAL_RESERVE
        };
        let next = self
            .used
            .checked_add(bytes)
            .filter(|n| *n <= ceiling)
            .ok_or_else(|| io::Error::other("diagnostic byte limit"))?;
        self.used = next;
        Ok(())
    }
    fn verify_identity(&self) -> io::Result<()> {
        let parent = std::fs::symlink_metadata(&self.parent_path)?;
        if !parent.is_dir()
            || (parent.dev(), parent.ino()) != self.parent_identity
            || parent.uid() != rustix::process::geteuid().as_raw()
            || parent.permissions().mode() & 0o077 != 0
        {
            return Err(io::Error::other("diagnostic parent path changed"));
        }
        let directory = rustix::fs::statat(
            &self.parent,
            &self.leaf,
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )?;
        if (directory.st_dev as u64, directory.st_ino) != self.directory_identity
            || directory.st_uid != rustix::process::geteuid().as_raw()
            || directory.st_mode & 0o077 != 0
        {
            return Err(io::Error::other("diagnostic directory path changed"));
        }
        for file in [
            &self.pre,
            &self.post,
            &self.render,
            &self.events,
            &self.pending,
            &self.result,
        ] {
            let meta = rustix::fs::statat(
                &self.directory,
                file.name,
                rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
            )?;
            if (meta.st_dev as u64, meta.st_ino) != (file.dev, file.ino)
                || meta.st_nlink != 1
                || meta.st_uid != rustix::process::geteuid().as_raw()
                || meta.st_mode & 0o077 != 0
            {
                return Err(io::Error::other("diagnostic artifact path changed"));
            }
        }
        Ok(())
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.pre.sync()?;
        self.post.sync()?;
        self.render.sync()?;
        self.events.sync()?;
        self.pending.sync()?;
        rustix::fs::fsync(&self.directory)?;
        Ok(())
    }
}
fn json_bytes(value: &impl Serialize) -> io::Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec(value).map_err(io::Error::other)?;
    if bytes.len() + 1 > MAX_LINE_BYTES {
        return Err(io::Error::other("diagnostic metadata line bound"));
    }
    bytes.push(b'\n');
    Ok(bytes)
}
fn pcm_bytes<'a, const N: usize>(samples: &[i16], bytes: &'a mut [u8; N]) -> &'a [u8] {
    for (sample, target) in samples.iter().zip(bytes.chunks_exact_mut(2)) {
        target.copy_from_slice(&sample.to_le_bytes());
    }
    &bytes[..samples.len() * 2]
}

#[derive(Clone, Default)]
struct Validation {
    next_record: u64,
    last_at_us: u64,
    capture: Option<(u64, u64, u64, u64)>,
    capture_playback_epoch: Option<u64>,
    render: Option<(u64, u64, u64)>,
    privacy: Option<PrivacyMeta>,
    capture_frames: u64,
    render_frames: u64,
    zeros: u64,
    boundaries: u64,
    written: u64,
    end: Option<EndMeta>,
}
impl Validation {
    fn accept(
        &mut self,
        envelope: &Envelope,
        maximum_seconds: u16,
        stream: StreamKind,
    ) -> Result<(), u64> {
        if envelope.sequence != self.next_record + 1 || self.end.is_some() {
            return Err(SEQUENCE_GAP);
        }
        let at = envelope.record.at_us();
        if at < self.last_at_us {
            return Err(INVALID_RECORD);
        }
        match envelope.record {
            Record::Capture(meta, ..) => {
                if self
                    .privacy
                    .is_none_or(|p| !p.open || p.generation != meta.privacy_generation)
                {
                    return Err(BOUNDARY_ERROR);
                }
                if let Some((epoch, dsp, privacy, next)) = self.capture {
                    if (epoch, dsp, privacy)
                        != (meta.epoch, meta.dsp_epoch, meta.privacy_generation)
                    {
                        return Err(BOUNDARY_ERROR);
                    }
                    if next != meta.frame_sequence {
                        return Err(SEQUENCE_GAP);
                    }
                } else if meta.frame_sequence != 1 {
                    return Err(SEQUENCE_GAP);
                }
                if self
                    .capture_playback_epoch
                    .is_some_and(|epoch| epoch != meta.reference.playback_epoch)
                {
                    return Err(BOUNDARY_ERROR);
                }
                self.capture_playback_epoch = Some(meta.reference.playback_epoch);
                self.capture = Some((
                    meta.epoch,
                    meta.dsp_epoch,
                    meta.privacy_generation,
                    meta.frame_sequence.checked_add(1).ok_or(SEQUENCE_GAP)?,
                ));
                self.capture_frames += 160;
                if self.capture_frames > u64::from(maximum_seconds) * 16000 {
                    return Err(DURATION_LIMIT);
                }
            }
            Record::Render(meta, frames, ..) | Record::Silence(meta, _, frames) => {
                if self
                    .privacy
                    .is_none_or(|p| !p.open || p.generation != meta.privacy_generation)
                {
                    return Err(BOUNDARY_ERROR);
                }
                if let Some((epoch, privacy, next)) = self.render {
                    if (epoch, privacy) != (meta.playback_epoch, meta.privacy_generation) {
                        return Err(BOUNDARY_ERROR);
                    }
                    if next != meta.first_sample {
                        return Err(CURSOR_GAP);
                    }
                } else if meta.first_sample != 0 {
                    return Err(CURSOR_GAP);
                }
                self.render = Some((
                    meta.playback_epoch,
                    meta.privacy_generation,
                    meta.end_sample,
                ));
                self.render_frames += u64::from(frames);
                if matches!(envelope.record, Record::Silence(..)) {
                    self.zeros += u64::from(frames);
                }
                if self.render_frames > u64::from(maximum_seconds) * 24000 {
                    return Err(DURATION_LIMIT);
                }
            }
            Record::Reset(meta) => {
                if self
                    .privacy
                    .is_none_or(|p| p.generation != meta.privacy_generation)
                {
                    return Err(BOUNDARY_ERROR);
                }
                match meta.reason {
                    ResetReason::Start => {
                        if self.capture.is_some() || self.render.is_some() {
                            return Err(BOUNDARY_ERROR);
                        }
                        match stream {
                            StreamKind::Capture if meta.capture_epoch > 0 && meta.dsp_epoch > 0 => {
                                self.capture = Some((
                                    meta.capture_epoch,
                                    meta.dsp_epoch,
                                    meta.privacy_generation,
                                    1,
                                ));
                                self.capture_playback_epoch =
                                    (meta.playback_epoch > 0).then_some(meta.playback_epoch);
                            }
                            StreamKind::Render if meta.playback_epoch > 0 => {
                                self.render =
                                    Some((meta.playback_epoch, meta.privacy_generation, 0))
                            }
                            _ => return Err(BOUNDARY_ERROR),
                        }
                    }
                    ResetReason::DspReset => {
                        if stream != StreamKind::Capture
                            || meta.capture_epoch == 0
                            || meta.dsp_epoch == 0
                        {
                            return Err(BOUNDARY_ERROR);
                        }
                        let next = if let Some((epoch, dsp, privacy, next)) = self.capture {
                            if epoch != meta.capture_epoch
                                || privacy != meta.privacy_generation
                                || meta.dsp_epoch <= dsp
                            {
                                return Err(BOUNDARY_ERROR);
                            }
                            next
                        } else {
                            1
                        };
                        self.capture = Some((
                            meta.capture_epoch,
                            meta.dsp_epoch,
                            meta.privacy_generation,
                            next,
                        ));
                        self.capture_playback_epoch =
                            (meta.playback_epoch > 0).then_some(meta.playback_epoch);
                    }
                    ResetReason::CaptureRestart => {
                        if stream != StreamKind::Capture
                            || meta.capture_epoch == 0
                            || meta.dsp_epoch == 0
                            || self
                                .capture
                                .is_some_and(|(epoch, ..)| meta.capture_epoch <= epoch)
                        {
                            return Err(BOUNDARY_ERROR);
                        }
                        self.capture = Some((
                            meta.capture_epoch,
                            meta.dsp_epoch,
                            meta.privacy_generation,
                            1,
                        ));
                        self.capture_playback_epoch =
                            (meta.playback_epoch > 0).then_some(meta.playback_epoch);
                    }
                    ResetReason::PlaybackReset => {
                        if stream != StreamKind::Render || meta.playback_epoch == 0 {
                            return Err(BOUNDARY_ERROR);
                        }
                        if let Some((epoch, _, cursor)) = self.render {
                            if meta.playback_epoch <= epoch
                                || !discard_matches(meta.discarded, epoch, cursor)
                            {
                                return Err(BOUNDARY_ERROR);
                            }
                        } else if meta.discarded.is_some() {
                            return Err(BOUNDARY_ERROR);
                        }
                        self.render = Some((meta.playback_epoch, meta.privacy_generation, 0));
                    }
                    ResetReason::Stop | ResetReason::Fault => {
                        if let Some((epoch, _, cursor)) = self.render
                            && meta.discarded.is_some()
                            && !discard_matches(meta.discarded, epoch, cursor)
                        {
                            return Err(BOUNDARY_ERROR);
                        }
                    }
                    ResetReason::PrivacyChange => {
                        // Observational only: privacy changes never erase PCM cursors.
                    }
                }
                self.boundaries += 1;
            }
            Record::Privacy(meta) => {
                if meta.generation == 0
                    || self.privacy.is_some_and(|old| {
                        meta.generation < old.generation
                            || (meta.open != old.open && meta.generation == old.generation)
                    })
                {
                    return Err(BOUNDARY_ERROR);
                }
                self.privacy = Some(meta);
                self.boundaries += 1;
            }
            Record::End(meta) => {
                self.end = Some(meta);
            }
        }
        self.next_record = envelope.sequence;
        self.last_at_us = at;
        self.written += 1;
        Ok(())
    }
}

fn discard_matches(discarded: Option<DiscardedRange>, epoch: u64, cursor: u64) -> bool {
    match discarded {
        Some(range) => {
            range.playback_epoch == epoch
                && range.retired_through <= range.accepted_through
                && range.accepted_through == cursor
        }
        None => cursor == 0,
    }
}

// Test-only fault injection is never present in an ordinary library build.
#[derive(Default)]
struct Hooks {
    #[cfg(test)]
    before_record: Option<Box<dyn FnMut() -> io::Result<()> + Send>>,
}
impl Hooks {
    fn before_record(&mut self) -> io::Result<()> {
        #[cfg(test)]
        if let Some(hook) = self.before_record.as_mut() {
            return hook();
        }
        Ok(())
    }
}
fn write_record(storage: &mut Storage, envelope: &Envelope) -> io::Result<()> {
    let (kind, meta, frames, zero_kind) = match envelope.record {
        Record::Capture(meta, ..) => ("capture", serde_json::to_value(meta), 160, None),
        Record::Render(meta, frames, ..) => (
            "render_accepted",
            serde_json::to_value(meta),
            usize::from(frames),
            None,
        ),
        Record::Silence(meta, kind, frames) => (
            "render_accepted",
            serde_json::to_value(meta),
            usize::from(frames),
            Some(kind),
        ),
        Record::Reset(meta) => ("reset", serde_json::to_value(meta), 0, None),
        Record::Privacy(meta) => ("privacy", serde_json::to_value(meta), 0, None),
        Record::End(meta) => ("end", serde_json::to_value(meta), 0, None),
    };
    let line = json_bytes(
        &json!({"sequence":envelope.sequence,"kind":kind,"meta":meta.map_err(io::Error::other)?,
        "frames":frames,"accepted_silence_kind":zero_kind,
        "pre_aec_byte_offset":storage.pre.bytes,"post_aec_byte_offset":storage.post.bytes,
        "render_accepted_byte_offset":storage.render.bytes}),
    )?;
    let pcm_size = match envelope.record {
        Record::Capture(..) => 640,
        Record::Render(_, n, ..) | Record::Silence(_, _, n) => u64::from(n) * 2,
        _ => 0,
    };
    storage.reserve(line.len() as u64 + pcm_size, false)?;
    match &envelope.record {
        Record::Capture(_, pre, post) => {
            let mut bytes = [0; 320];
            storage.pre.write(pcm_bytes(pre, &mut bytes))?;
            storage.post.write(pcm_bytes(post, &mut bytes))?;
        }
        Record::Render(_, frames, samples) => {
            let mut bytes = [0; 480];
            storage
                .render
                .write(pcm_bytes(&samples[..usize::from(*frames)], &mut bytes))?;
        }
        Record::Silence(_, _, frames) => storage
            .render
            .write(&[0; 480][..usize::from(*frames) * 2])?,
        _ => {}
    }
    storage.events.write(&line)
}
fn run_writer(
    mut consumer: Consumer<Envelope>,
    mut storage: Storage,
    shared: Arc<Shared>,
    config: Config,
    started_at_us: u64,
    mut hooks: Hooks,
) -> Acknowledgement {
    let mut state = Validation::default();
    let mut error = None;
    loop {
        if lamp_ipc::monotonic_us().saturating_sub(started_at_us)
            > u64::from(config.max_seconds) * 1_000_000
        {
            shared.faults.fetch_or(DURATION_LIMIT, Ordering::Release);
            break;
        }
        match consumer.pop() {
            Ok(envelope) => {
                let mut next = state.clone();
                if let Err(fault) = next.accept(&envelope, config.max_seconds, config.stream) {
                    shared.faults.fetch_or(fault, Ordering::Release);
                    break;
                }
                if let Err(failure) = hooks
                    .before_record()
                    .and_then(|()| write_record(&mut storage, &envelope))
                {
                    let fault = if failure.to_string() == "diagnostic byte limit" {
                        BYTE_LIMIT
                    } else {
                        WRITER_ERROR
                    };
                    shared.faults.fetch_or(fault, Ordering::Release);
                    error = Some(short_error(&failure));
                    break;
                }
                state = next;
            }
            Err(_) if shared.closed.load(Ordering::Acquire) || consumer.is_abandoned() => break,
            Err(_) if shared.faults.load(Ordering::Acquire) != 0 => break,
            Err(_) => thread::sleep(Duration::from_millis(1)),
        }
    }
    if state.end.is_some_and(|end| end.reason == EndReason::Fault) {
        shared.faults.fetch_or(AUDIO_FAULT, Ordering::Release);
    }
    if state.end.is_none() || state.written != shared.queued.load(Ordering::Acquire) {
        shared.faults.fetch_or(ABANDONED, Ordering::Release);
    }
    if state.capture_frames + state.render_frames == 0 {
        shared.faults.fetch_or(INVALID_RECORD, Ordering::Release);
    }
    if let Err(failure) = storage.sync_all() {
        shared.faults.fetch_or(WRITER_ERROR, Ordering::Release);
        error = Some(short_error(&failure));
    }
    if let Err(failure) = storage.verify_identity() {
        shared.faults.fetch_or(PATH_CHANGED, Ordering::Release);
        error = Some(short_error(&failure));
    }
    let mut summary = Summary {
        valid: shared.faults.load(Ordering::Acquire) == 0,
        boot: config.boot,
        stream: config.stream,
        software_processing: config.software_processing,
        started_at_us,
        finalized_at_us: lamp_ipc::monotonic_us(),
        faults: shared.faults.load(Ordering::Acquire),
        records_queued: shared.queued.load(Ordering::Acquire),
        records_written: state.written,
        records_rejected: shared.rejected.load(Ordering::Acquire),
        capture_frames: state.capture_frames,
        render_frames: state.render_frames,
        accepted_zero_frames: state.zeros,
        boundaries: state.boundaries,
        bytes_reserved: storage.used,
        pre_aec_sha256: storage.pre.hash(),
        post_aec_sha256: storage.post.hash(),
        render_accepted_sha256: storage.render.hash(),
        events_sha256: storage.events.hash(),
        end: state.end,
        writer_error: error,
    };
    let mut marker_published = false;
    let mut manifest_sha256 = None;
    let publication = (|| -> io::Result<()> {
        // Find the exact self-inclusive serialized byte count before any final write.
        for _ in 0..4 {
            let size = json_bytes(&summary)?.len() as u64;
            let total = storage.used + size;
            if summary.bytes_reserved == total {
                break;
            }
            summary.bytes_reserved = total;
        }
        let bytes = json_bytes(&summary)?;
        if summary.bytes_reserved != storage.used + bytes.len() as u64 {
            return Err(io::Error::other("final metadata size did not stabilize"));
        }
        storage.reserve(bytes.len() as u64, true)?;
        storage.result.write(&bytes)?;
        storage.result.sync()?;
        manifest_sha256 = Some(storage.result.hash());
        if let Err(error) = storage.verify_identity() {
            shared.faults.fetch_or(PATH_CHANGED, Ordering::Release);
            return Err(error);
        }
        if summary.valid && shared.faults.load(Ordering::Acquire) == 0 {
            // Hard linking the fully flushed result is atomic and never overwrites
            // an existing marker. A finish timeout racing this native call still
            // invalidates the run: readers MUST require its successful acknowledgement.
            rustix::fs::linkat(
                &storage.directory,
                "result.json",
                &storage.directory,
                "complete.json",
                rustix::fs::AtFlags::empty(),
            )?;
            rustix::fs::fsync(&storage.directory)?;
            marker_published = true;
        }
        Ok(())
    })();
    if let Err(failure) = publication {
        shared.faults.fetch_or(WRITER_ERROR, Ordering::Release);
        summary.writer_error = Some(short_error(&failure));
    }
    summary.faults = shared.faults.load(Ordering::Acquire);
    summary.valid = summary.valid && summary.faults == 0 && marker_published;
    Acknowledgement {
        summary,
        marker_published,
        manifest_sha256,
    }
}

fn short_error(error: &io::Error) -> String {
    error.to_string().chars().take(512).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        sync::{Condvar, Mutex},
        time::Instant,
    };
    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Private(PathBuf);
    impl Private {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "lamp-diag-writer-test-{}-{}-{}",
                std::process::id(),
                lamp_ipc::monotonic_ns(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }
        fn config(&self) -> Config {
            Config::new(
                self.0.join("audio"),
                BootId::new([9; 16]).unwrap(),
                StreamKind::Capture,
            )
        }
    }
    impl Drop for Private {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn capture(at: u64, sequence: u64) -> Record {
        Record::Capture(
            CaptureMeta {
                epoch: 1,
                dsp_epoch: 1,
                privacy_generation: 1,
                frame_sequence: sequence,
                first_read_started_at_us: at,
                last_read_started_at_us: at,
                read_completed_at_us: at,
                successful_reads: 1,
                processing_completed_at_us: at,
                vad_score: 0.5,
                ..CaptureMeta::default()
            },
            [1; 160],
            [2; 160],
        )
    }
    fn open(at: u64) -> Record {
        Record::Privacy(PrivacyMeta {
            at_us: at,
            generation: 1,
            open: true,
        })
    }
    struct Gate(Arc<(Mutex<bool>, Condvar)>);
    impl Drop for Gate {
        fn drop(&mut self) {
            let (mut guard, wake) = (self.0.0.lock().unwrap(), &self.0.1);
            *guard = true;
            wake.notify_all();
        }
    }

    #[test]
    fn stalled_writer_queue_overflow_and_finish_timeout_never_wait_for_join() {
        let temp = Private::new();
        let latch = Arc::new((Mutex::new(false), Condvar::new()));
        let gate = Gate(latch.clone());
        let (tx, rx) = mpsc::sync_channel(1);
        let mut notified = false;
        let hooks = Hooks {
            before_record: Some(Box::new(move || {
                if !notified {
                    notified = true;
                    tx.try_send(()).unwrap();
                }
                let mut ready = latch.0.lock().unwrap();
                while !*ready {
                    ready = latch.1.wait(ready).unwrap();
                }
                Ok(())
            })),
        };
        let mut recorder = Recorder::start_inner(temp.config(), hooks).unwrap();
        let at = recorder.started_at_us;
        assert_eq!(recorder.submit(open(at)), SubmitResult::Queued);
        rx.recv_timeout(Duration::from_secs(1)).unwrap();
        for sequence in 1..=QUEUE_RECORDS as u64 {
            assert_eq!(recorder.submit(capture(at, sequence)), SubmitResult::Queued);
        }
        assert_eq!(recorder.submit(capture(at, 65)), SubmitResult::Full);
        assert_ne!(recorder.faults() & OVERFLOW, 0);
        let began = Instant::now();
        let report = recorder.finish(
            EndMeta {
                at_us: at,
                reason: EndReason::Completed,
            },
            Duration::from_millis(25),
        );
        assert_eq!(report.outcome, FinishOutcome::TimedOut);
        assert!(!report.valid);
        assert!(began.elapsed() < Duration::from_millis(500));
        assert!(!temp.0.join("audio/complete.json").exists());
        drop(gate);
    }

    #[test]
    fn failed_writer_keeps_pending_and_returns_invalid_without_marker() {
        let temp = Private::new();
        let hooks = Hooks {
            before_record: Some(Box::new(|| {
                Err(io::Error::other("injected writer failure"))
            })),
        };
        let mut recorder = Recorder::start_inner(temp.config(), hooks).unwrap();
        let at = recorder.started_at_us;
        recorder.submit(open(at));
        recorder.submit(capture(at, 1));
        let report = recorder.finish(
            EndMeta {
                at_us: at,
                reason: EndReason::Completed,
            },
            DEFAULT_FINISH_WAIT,
        );
        assert!(!report.valid);
        assert_ne!(report.faults & WRITER_ERROR, 0);
        assert_eq!(report.summary.unwrap().records_written, 0);
        assert!(temp.0.join("audio/manifest.pending.json").exists());
        assert!(!temp.0.join("audio/complete.json").exists());
    }

    #[test]
    fn byte_cap_counts_pcm_metadata_and_final_report_without_queue_overflow() {
        let temp = Private::new();
        let mut config = temp.config();
        config.max_bytes = 2 * FINAL_RESERVE;
        let at = lamp_ipc::monotonic_us();
        let storage = Storage::create(&config, at).unwrap();
        let (mut producer, consumer) = RingBuffer::new(QUEUE_RECORDS);
        producer
            .push(Envelope {
                sequence: 1,
                record: open(at),
            })
            .ok()
            .unwrap();
        for sequence in 1..=10 {
            producer
                .push(Envelope {
                    sequence: sequence + 1,
                    record: capture(at, sequence),
                })
                .ok()
                .unwrap();
        }
        producer
            .push(Envelope {
                sequence: 12,
                record: Record::End(EndMeta {
                    at_us: at,
                    reason: EndReason::Completed,
                }),
            })
            .ok()
            .unwrap();
        let shared = Arc::new(Shared::default());
        shared.queued.store(12, Ordering::Release);
        shared.closed.store(true, Ordering::Release);
        drop(producer);
        let ack = run_writer(consumer, storage, shared, config, at, Hooks::default());
        assert!(!ack.summary.valid);
        assert_ne!(ack.summary.faults & BYTE_LIMIT, 0);
        assert_eq!(ack.summary.faults & OVERFLOW, 0);
        assert!(ack.summary.bytes_reserved <= 2 * FINAL_RESERVE);
        let total: u64 = fs::read_dir(temp.0.join("audio"))
            .unwrap()
            .map(|entry| entry.unwrap().metadata().unwrap().len())
            .sum();
        assert!(total <= 2 * FINAL_RESERVE);
        assert!(!temp.0.join("audio/complete.json").exists());
    }

    #[test]
    fn duration_sample_budget_does_not_trust_static_timestamps() {
        let mut state = Validation::default();
        state
            .accept(
                &Envelope {
                    sequence: 1,
                    record: open(1),
                },
                1,
                StreamKind::Capture,
            )
            .unwrap();
        for sequence in 1..=100 {
            state
                .accept(
                    &Envelope {
                        sequence: sequence + 1,
                        record: capture(1, sequence),
                    },
                    1,
                    StreamKind::Capture,
                )
                .unwrap();
        }
        assert_eq!(
            state.accept(
                &Envelope {
                    sequence: 102,
                    record: capture(1, 101)
                },
                1,
                StreamKind::Capture
            ),
            Err(DURATION_LIMIT)
        );
    }

    #[test]
    fn incomplete_drop_and_out_of_order_record_can_never_complete() {
        let mut state = Validation::default();
        assert_eq!(
            state.accept(
                &Envelope {
                    sequence: 2,
                    record: open(1)
                },
                1,
                StreamKind::Capture
            ),
            Err(SEQUENCE_GAP)
        );
        let temp = Private::new();
        let recorder = Recorder::start(temp.config()).unwrap();
        let shared = recorder.shared.clone();
        drop(recorder);
        assert_ne!(shared.faults.load(Ordering::Acquire) & ABANDONED, 0);
        assert!(!temp.0.join("audio/complete.json").exists());
    }
}
