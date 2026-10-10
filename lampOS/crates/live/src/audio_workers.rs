//! Dedicated Linux audio loops with actual accepted PCM and a direct reference link.
use crate::{
    activity::SpeechProbability,
    diagnostic_control::{DiagnosticFinished, DiagnosticStarted},
    diagnostics::{self, EndReason, Recorder, ResetMeta, ResetReason, StreamKind},
    options::{NoiseSuppression, SoftwareProcessing},
    playback::SpeechPlayback,
    reference::{PRIME_FRAMES, ReferenceAssembly, accepted_write_survived},
    transport::{ReferenceChannels, WorkerChannels},
    wire::{
        CaptureTiming, ClockPrimed, Control, MicrophoneFrame, ReferenceControl, ReferenceFault,
        ReferenceFaultKind, ReferenceTiming, RenderPayload, RenderReference, SpeakerChunk,
        WorkerEvent,
    },
};
use lamp_audio::{
    EchoProcessor, RENDER_SAMPLES,
    capture::{CaptureConfig, ErrorKind as CaptureErrorKind, Microphone},
    pcm::{Speaker, Written},
};
use lamp_interaction::{
    BootId, Error as InteractionError, MonoTime, Permission, Snapshot, TurnOwner,
};
use lamp_ipc::monotonic_us;
use std::{
    io,
    path::Path,
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
const TICK: Duration = Duration::from_millis(2);
const MAX_CONTROL: usize = 8;
const MAX_REFERENCE: usize = 16;
const BARRIER_US: u64 = 100_000;

struct Recording {
    recorder: Recorder,
    started: DiagnosticStarted,
    privacy: Option<(u64, bool)>,
}
impl Recording {
    fn start(
        path: Option<&Path>,
        boot: BootId,
        stream: StreamKind,
        software_processing: Option<SoftwareProcessing>,
        channels: &mut WorkerChannels,
    ) -> Result<Option<Self>> {
        let Some(path) = path else {
            return Ok(None);
        };
        let mut config = diagnostics::Config::new(path.to_owned(), boot, stream);
        config.software_processing = software_processing;
        let recorder = Recorder::start(config)?;
        let started = DiagnosticStarted {
            boot,
            stream: stream.into(),
            started_at_us: recorder.started_at_us(),
        };
        channels
            .control
            .send(WorkerEvent::DiagnosticsStarted { started })?;
        Ok(Some(Self {
            recorder,
            started,
            privacy: None,
        }))
    }
    fn observe_privacy(&mut self, snapshot: Snapshot) {
        let observed = (
            snapshot.microphone_generation(),
            snapshot.microphone_permission() == Permission::Allowed,
        );
        if self.privacy != Some(observed) {
            let _ = self.recorder.try_privacy(diagnostics::PrivacyMeta {
                at_us: monotonic_us(),
                generation: observed.0,
                open: observed.1,
            });
            self.privacy = Some(observed);
        }
    }
    fn render(
        &mut self,
        written: Written,
        owner: Option<TurnOwner>,
        sequence: Option<u64>,
    ) -> diagnostics::RenderMeta {
        diagnostics::RenderMeta {
            playback_epoch: written.first_sample.epoch,
            privacy_generation: self.privacy.map_or(0, |p| p.0),
            first_sample: written.first_sample.frame,
            end_sample: written.end_sample.frame,
            accepted_at_us: written.completed_at_us,
            queue_observed_at_us: written.queue_observed_at_us,
            queued_frames: written.queued_frames,
            owner,
            chunk_sequence: sequence,
        }
    }
    fn finish(self, channels: &mut WorkerChannels, reason: EndReason) -> Result<()> {
        let report = self.recorder.finish(
            diagnostics::EndMeta {
                at_us: monotonic_us(),
                reason,
            },
            diagnostics::DEFAULT_FINISH_WAIT,
        );
        let valid = report.valid;
        channels.control.send(WorkerEvent::DiagnosticsFinished {
            receipt: DiagnosticFinished::from_report(self.started, report),
        })?;
        if !valid {
            return Err(
                io::Error::other("audio diagnostic recording did not finish validly").into(),
            );
        }
        Ok(())
    }
}

fn discarded_range(range: lamp_audio::ownership::QueuedRange) -> diagnostics::DiscardedRange {
    diagnostics::DiscardedRange {
        playback_epoch: range.epoch,
        retired_through: range.retired_through,
        accepted_through: range.accepted_through,
    }
}

fn diagnostic_reset(
    recording: &mut Option<Recording>,
    reason: ResetReason,
    capture_epoch: u64,
    dsp_epoch: u64,
    playback_epoch: u64,
    privacy_generation: u64,
    discarded: Option<lamp_audio::ownership::QueuedRange>,
) {
    if let Some(recording) = recording {
        let _ = recording.recorder.try_reset(ResetMeta {
            at_us: monotonic_us(),
            reason,
            capture_epoch,
            dsp_epoch,
            playback_epoch,
            privacy_generation,
            discarded: discarded.map(discarded_range),
        });
    }
}

pub fn capture(
    channels: WorkerChannels,
    controller: BootId,
    worker: BootId,
    directory: &Path,
    device: &str,
    diagnostic_directory: Option<&Path>,
) -> Result<()> {
    capture_with_noise_suppression(
        channels,
        controller,
        worker,
        directory,
        device,
        diagnostic_directory,
        NoiseSuppression::On,
    )
}

pub fn capture_with_noise_suppression(
    mut channels: WorkerChannels,
    controller: BootId,
    worker: BootId,
    directory: &Path,
    device: &str,
    diagnostic_directory: Option<&Path>,
    noise_suppression: NoiseSuppression,
) -> Result<()> {
    let mut recording = Recording::start(
        diagnostic_directory,
        controller,
        StreamKind::Capture,
        Some(noise_suppression.software_processing()),
        &mut channels,
    )?;
    let mut microphone = Microphone::new(device, controller, CaptureConfig::default())?;
    let result = capture_loop(
        &mut channels,
        &mut microphone,
        worker,
        directory,
        &mut recording,
        noise_suppression,
    );
    // Invalidate input authority and close hardware before any filesystem acknowledgement wait.
    let close = microphone.control_lost();
    drop(microphone);
    if let Err(error) = &result {
        report_fault(&mut channels, error.as_ref());
    }
    if let Err(error) = &close {
        report_fault(&mut channels, error);
    }
    let reason = if close.is_err() {
        EndReason::Fault
    } else {
        result.as_ref().copied().unwrap_or(EndReason::Fault)
    };
    let finish = recording.map_or(Ok(()), |r| r.finish(&mut channels, reason));
    result
        .map(|_| ())
        .and_then(|()| close.map_err(Into::into))
        .and(finish)
}

fn capture_loop(
    channels: &mut WorkerChannels,
    microphone: &mut Microphone,
    worker: BootId,
    directory: &Path,
    recording: &mut Option<Recording>,
    noise_suppression: NoiseSuppression,
) -> Result<EndReason> {
    let mut link: Option<ReferenceChannels> = None;
    let mut prepare_requested = false;
    let mut capture_prepared = false;
    let mut privacy = 0;
    let mut reference = ReferenceAssembly::default();
    let mut echo = EchoProcessor::new(noise_suppression.enabled());
    let mut speech = SpeechProbability::default();
    let mut dsp_epoch = 1u64;
    let mut last_diagnostic = 0;
    let mut primed_at = None;
    channels.control.send(WorkerEvent::Ready)?;
    loop {
        let tick = Instant::now();
        for index in 0..=MAX_CONTROL {
            let Some(control) = channels.control.receive::<Control>()? else {
                break;
            };
            if index == MAX_CONTROL {
                return Err(io::Error::other("capture control tick bound").into());
            }
            match control {
                Control::Authority { snapshot } => match microphone.install(snapshot) {
                    Ok(update) => {
                        if let Some(recording) = recording {
                            recording.observe_privacy(snapshot);
                        }
                        if update.closed {
                            channels.control.send(WorkerEvent::CaptureStopped)?;
                            return Ok(EndReason::PrivacyClosed);
                        }
                    }
                    Err(error) if matches!(error.kind, CaptureErrorKind::Authority(reason) if rejected_old_state(reason)) =>
                        {}
                    Err(error) => return Err(error.into()),
                },
                Control::ConnectReference { peer } => {
                    if link.is_some() {
                        return Err(io::Error::other("reference peer already bound").into());
                    }
                    link = Some(ReferenceChannels::bind(directory, "capture", worker, peer)?);
                }
                Control::StartCapture => {
                    if prepare_requested {
                        return Err(io::Error::other("capture start already requested").into());
                    }
                    prepare_requested = true;
                }
                Control::Stop => {
                    microphone.revoke()?;
                    channels.control.send(WorkerEvent::CaptureStopped)?;
                    return Ok(EndReason::Stopped);
                }
            }
        }
        microphone.maintain()?;
        if let Some(link) = link.as_mut()
            && link.connect_step()?
        {
            if prepare_requested && !capture_prepared {
                microphone.reset()?;
                let report = microphone.prepare()?;
                privacy = report.microphone_generation;
                capture_prepared = true;
                channels.control.send(WorkerEvent::CapturePrepared {
                    epoch: report.epoch,
                    privacy_generation: privacy,
                    prepared_at_us: report.prepared_at_us,
                })?;
                link.control.send(ReferenceControl::CapturePrepared {
                    privacy_generation: privacy,
                })?;
            }
            let mut control_count = 0;
            // Receive data first, then recheck priority lifecycle before using
            // it: two sockets can become readable between separate drains.
            for data_index in 0..=MAX_REFERENCE {
                let packet = link.data.receive::<RenderReference>()?;
                for _ in 0..=MAX_CONTROL {
                    let Some(control) = link.control.receive::<ReferenceControl>()? else {
                        break;
                    };
                    if control_count == MAX_CONTROL {
                        return Err(io::Error::other("reference control tick bound").into());
                    }
                    control_count += 1;
                    match control {
                        ReferenceControl::ClockPrimed { prime } => {
                            if !capture_prepared {
                                return Err(reference
                                    .fault(ReferenceFaultKind::InvalidPrime, monotonic_us())
                                    .into());
                            }
                            let previous = reference.timing().playback_epoch;
                            reference.prime(prime, monotonic_us(), privacy, &mut echo)?;
                            speech.reset();
                            if previous != 0 {
                                dsp_epoch = dsp_epoch
                                    .checked_add(1)
                                    .ok_or_else(|| io::Error::other("DSP epoch exhausted"))?;
                                channels.control.send(WorkerEvent::CaptureDspReset {
                                    epoch: microphone.epoch(),
                                    dsp_epoch,
                                    observed_at_us: monotonic_us(),
                                })?;
                            }
                            diagnostic_reset(
                                recording,
                                if previous == 0 {
                                    ResetReason::Start
                                } else {
                                    ResetReason::DspReset
                                },
                                microphone.epoch(),
                                dsp_epoch,
                                prime.playback_epoch,
                                privacy,
                                prime.discarded,
                            );
                            // No microphone sequence or retained upstream prefix is reset.
                            primed_at = Some(monotonic_us());
                            link.control.send(ReferenceControl::ReferencePrimed {
                                playback_epoch: prime.playback_epoch,
                                analysed_through: reference.timing().analysed_through,
                            })?;
                        }
                        ReferenceControl::ClockStarted {
                            playback_epoch,
                            start_requested_at_us,
                            started_at_us,
                        } => {
                            reference.started(
                                playback_epoch,
                                start_requested_at_us,
                                started_at_us,
                                monotonic_us(),
                            )?;
                            primed_at = None;
                            if !microphone.is_running() {
                                let report = microphone.start_prepared()?;
                                channels.control.send(WorkerEvent::CaptureStarted {
                                    epoch: report.epoch,
                                    dsp_epoch,
                                    noise_suppression,
                                    privacy_generation: privacy,
                                    rate: report.format.rate,
                                    channels: report.format.channels,
                                    period_frames: report.format.period_frames,
                                    buffer_frames: report.format.buffer_frames,
                                    monotonic_status_timestamps: report
                                        .format
                                        .monotonic_status_timestamps,
                                    open_started_at_us: report.open_started_at_us,
                                    prepared_at_us: report.prepared_at_us,
                                    capture_started_at_us: report.capture_started_at_us,
                                    start_completed_at_us: report.start_completed_at_us,
                                })?;
                            }
                        }
                        ReferenceControl::Stopped { .. } => {
                            microphone.revoke()?;
                            channels.control.send(WorkerEvent::CaptureStopped)?;
                            return Ok(EndReason::Stopped);
                        }
                        _ => {
                            return Err(io::Error::other(
                                "wrong direct reference command for capture",
                            )
                            .into());
                        }
                    }
                }
                let Some(packet) = packet else {
                    break;
                };
                if data_index == MAX_REFERENCE {
                    return Err(reference
                        .fault(ReferenceFaultKind::ReferenceBacklog, monotonic_us())
                        .into());
                }
                if !microphone.is_open() {
                    return Err(reference
                        .fault(ReferenceFaultKind::PrivacyMismatch, monotonic_us())
                        .into());
                }
                reference.accept(packet, monotonic_us(), privacy, &mut echo)?;
            }
        }
        if primed_at.is_some_and(|at| monotonic_us().saturating_sub(at) >= BARRIER_US) {
            return Err(reference
                .fault(ReferenceFaultKind::BarrierTimeout, monotonic_us())
                .into());
        }
        if microphone.is_running()
            && let Some(frame) = microphone.try_read()?
        {
            let delay = reference.before_capture(frame.timing.delayed_frames, monotonic_us())?;
            let clean = echo.capture(&frame.samples, delay)?;
            reference.captured(frame.timing.read_completed_at_us, monotonic_us())?;
            let probability = speech.analyze(&clean);
            if !probability.is_finite() || !(0.0..=1.0).contains(&probability) {
                return Err(io::Error::other("invalid local speech probability").into());
            }
            microphone.maintain()?;
            let timing = reference.timing();
            let processing_completed_at_us = monotonic_us();
            if let Some(recording) = recording {
                let _ = recording.recorder.try_capture(
                    diagnostics::CaptureMeta {
                        epoch: frame.epoch,
                        dsp_epoch,
                        privacy_generation: frame.microphone_generation,
                        frame_sequence: frame.sequence,
                        first_read_started_at_us: frame.timing.first_read_started_at_us,
                        last_read_started_at_us: frame.timing.last_read_started_at_us,
                        read_completed_at_us: frame.timing.read_completed_at_us,
                        successful_reads: frame.timing.successful_reads,
                        alsa_status_monotonic_us: frame.timing.alsa_status_monotonic_us,
                        status_observed_at_us: frame.timing.status_observed_at_us,
                        available_frames: frame.timing.available_frames,
                        delayed_frames: frame.timing.delayed_frames,
                        processing_completed_at_us,
                        aec_queue_delay_ms: delay,
                        vad_score: probability,
                        reference: diagnostics::ReferenceMeta {
                            playback_epoch: timing.playback_epoch,
                            accepted_through: timing.accepted_through,
                            analysed_through: timing.analysed_through,
                            capture_blocks_processed: timing.capture_blocks_processed,
                            capture_blocks_before_clock_start: timing
                                .capture_blocks_before_clock_start,
                            accepted_at_us: timing.accepted_at_us,
                            analysed_at_us: timing.analysed_at_us,
                            queue_observed_at_us: timing.queue_observed_at_us,
                            queued_frames: timing.queued_frames as u64,
                            clock_start_requested_at_us: timing.clock_start_requested_at_us,
                            clock_started_at_us: timing.clock_started_at_us,
                            ignored_old_epoch_packets: timing.ignored_old_epoch_packets,
                        },
                    },
                    &frame.samples,
                    &clean,
                );
            }
            channels.data.send(MicrophoneFrame {
                epoch: frame.epoch,
                dsp_epoch,
                privacy_generation: frame.microphone_generation,
                frame_sequence: frame.sequence,
                read_completed_at_us: frame.timing.read_completed_at_us,
                timing: CaptureTiming {
                    first_read_started_at_us: frame.timing.first_read_started_at_us,
                    last_read_started_at_us: frame.timing.last_read_started_at_us,
                    successful_reads: frame.timing.successful_reads,
                    alsa_status_monotonic_us: frame.timing.alsa_status_monotonic_us,
                    status_observed_at_us: frame.timing.status_observed_at_us,
                    available_frames: frame.timing.available_frames,
                    delayed_frames: frame.timing.delayed_frames,
                    processing_completed_at_us,
                    aec_queue_delay_ms: delay,
                    reference: timing,
                },
                probability,
                samples: clean.to_vec(),
            })?;
            if monotonic_us().saturating_sub(last_diagnostic) >= 1_000_000 {
                channels
                    .control
                    .send(WorkerEvent::ReferenceClock { timing })?;
                last_diagnostic = monotonic_us();
            }
        }
        finish_tick(tick);
    }
}

pub fn speaker(
    mut channels: WorkerChannels,
    controller: BootId,
    worker: BootId,
    directory: &Path,
    device: &str,
    diagnostic_directory: Option<&Path>,
) -> Result<()> {
    if device.is_empty()
        || device.len() > 128
        || device.bytes().any(|b| b == 0 || b.is_ascii_control())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "explicit valid speaker alias required",
        )
        .into());
    }
    let mut recording = Recording::start(
        diagnostic_directory,
        controller,
        StreamKind::Render,
        None,
        &mut channels,
    )?;
    let mut speaker = Speaker::open_clock(device, controller)?;
    let result = speaker_loop(
        &mut channels,
        &mut speaker,
        worker,
        directory,
        &mut recording,
    );
    // Physical drop and permanent authority invalidation precede diagnostic finalization.
    let stop = speaker.fault();
    drop(speaker);
    if let Err(error) = &result {
        report_fault(&mut channels, error.as_ref());
    }
    if let Err(error) = &stop {
        report_fault(&mut channels, error.as_ref());
    }
    let reason = if stop.is_err() {
        EndReason::Fault
    } else {
        result.as_ref().copied().unwrap_or(EndReason::Fault)
    };
    let finish = recording.map_or(Ok(()), |r| r.finish(&mut channels, reason));
    result.map(|_| ()).and(stop).and(finish)
}
struct PendingChunk {
    chunk: SpeakerChunk,
    offset: usize,
}
#[derive(Default)]
struct ReferenceOutput {
    sequence: u64,
    privacy: Option<u64>,
    primed_at: Option<u64>,
    prime_last_write: Option<Written>,
    waiting_ack: bool,
}
impl ReferenceOutput {
    fn reset(&mut self) {
        self.sequence = 0;
        self.primed_at = None;
        self.prime_last_write = None;
        self.waiting_ack = false;
    }
    fn fault(&self, speaker: &Speaker, reason: ReferenceFaultKind) -> ReferenceFault {
        let range = speaker.queued_range();
        ReferenceFault {
            reason,
            at_us: monotonic_us(),
            expected_sequence: self.sequence.saturating_add(1),
            received_sequence: None,
            received_first_sample: None,
            timing: ReferenceTiming {
                playback_epoch: range.epoch,
                accepted_through: range.accepted_through,
                queue_observed_at_us: speaker.last_poll().map_or(0, |s| s.observed_at_us),
                queued_frames: speaker.last_poll().map_or(0, |s| s.queued_frames),
                ..ReferenceTiming::default()
            },
        }
    }
    fn prime(
        &mut self,
        speaker: &mut Speaker,
        link: &mut ReferenceChannels,
        recording: &mut Option<Recording>,
    ) -> Result<()> {
        if self.waiting_ack || speaker.clock_started() {
            return Ok(());
        }
        let token = speaker.zero_authority()?;
        if Some(token.microphone_generation()) != self.privacy {
            return Err(self
                .fault(speaker, ReferenceFaultKind::PrivacyMismatch)
                .into());
        }
        if self.primed_at.is_none() && speaker.queued_range().epoch == 1 {
            diagnostic_reset(
                recording,
                ResetReason::Start,
                0,
                0,
                1,
                token.microphone_generation(),
                None,
            );
        }
        self.primed_at.get_or_insert_with(monotonic_us);
        let remaining =
            PRIME_FRAMES.saturating_sub(speaker.queued_range().accepted_through as usize);
        if remaining > 0 {
            let written = speaker.try_write_silence(token, remaining.min(RENDER_SAMPLES))?;
            if written.frames > 0 {
                if let Some(recording) = recording {
                    let meta = recording.render(written, None, None);
                    let _ = recording.recorder.try_silence(
                        meta,
                        diagnostics::SilenceKind::Prime,
                        written.frames as u16,
                    );
                }
                self.prime_last_write = Some(written);
            }
        }
        if speaker.queued_range().accepted_through == PRIME_FRAMES as u64 {
            let last = self
                .prime_last_write
                .ok_or_else(|| self.fault(speaker, ReferenceFaultKind::InvalidPrime))?;
            if last.queued_frames != PRIME_FRAMES as i64 {
                return Err(self.fault(speaker, ReferenceFaultKind::InvalidPrime).into());
            }
            link.control.send(ReferenceControl::ClockPrimed {
                prime: ClockPrimed {
                    playback_epoch: speaker.queued_range().epoch,
                    privacy_generation: token.microphone_generation(),
                    accepted_zero_frames: PRIME_FRAMES,
                    accepted_at_us: last.completed_at_us,
                    queue_observed_at_us: last.queue_observed_at_us,
                    queued_frames: last.queued_frames as usize,
                    discarded: speaker.last_discard(),
                },
            })?;
            self.waiting_ack = true;
        }
        Ok(())
    }
    fn send(
        &mut self,
        link: &mut ReferenceChannels,
        written: Written,
        payload: RenderPayload,
    ) -> Result<()> {
        let sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| io::Error::other("render sequence exhausted"))?;
        link.data.send(RenderReference {
            playback_epoch: written.first_sample.epoch,
            privacy_generation: self
                .privacy
                .ok_or_else(|| io::Error::other("missing reference privacy"))?,
            sequence,
            first_sample: written.first_sample.frame,
            accepted_at_us: written.completed_at_us,
            queue_observed_at_us: written.queue_observed_at_us,
            queued_frames: written.queued_frames as usize,
            payload,
        })?;
        self.sequence = sequence;
        Ok(())
    }
}

fn speaker_loop(
    channels: &mut WorkerChannels,
    speaker: &mut Speaker,
    worker: BootId,
    directory: &Path,
    recording: &mut Option<Recording>,
) -> Result<EndReason> {
    let mut link: Option<ReferenceChannels> = None;
    let mut output = ReferenceOutput::default();
    let mut pending: Option<PendingChunk> = None;
    let mut speech = SpeechPlayback::default();
    let mut last_sequence = 0;
    let mut flush_count = speaker.flush_count();
    channels.control.send(WorkerEvent::Ready)?;
    loop {
        let tick = Instant::now();
        for index in 0..=MAX_CONTROL {
            let Some(control) = channels.control.receive::<Control>()? else {
                break;
            };
            if index == MAX_CONTROL {
                return Err(io::Error::other("speaker control tick bound").into());
            }
            match control {
                Control::Authority { snapshot } => {
                    if let Err(error) = speaker.install(snapshot) {
                        if error
                            .downcast_ref::<InteractionError>()
                            .is_some_and(|reason| rejected_old_state(*reason))
                        {
                            continue;
                        }
                        if snapshot.microphone_permission() == Permission::Allowed
                            || error.downcast_ref::<InteractionError>()
                                != Some(&InteractionError::PrivacyClosed)
                        {
                            return Err(error);
                        }
                    }
                    if let Some(recording) = recording {
                        recording.observe_privacy(snapshot);
                    }
                    if output.privacy.is_some()
                        && snapshot.microphone_permission() != Permission::Allowed
                    {
                        // install may already have dropped queued PCM. Preserve
                        // that original discarded range instead of replacing it
                        // with a second empty drop before its diagnostic receipt.
                        if speaker.flush_count() == flush_count {
                            speaker.stop()?;
                        }
                        report_speech_reset(channels, &mut speech)?;
                        diagnostic_reset(
                            recording,
                            ResetReason::PrivacyChange,
                            0,
                            0,
                            speaker.queued_range().epoch,
                            snapshot.microphone_generation(),
                            speaker.last_discard(),
                        );
                        if let Some(link) = link.as_mut() {
                            link.notify_stopped_after_local_close(speaker.queued_range().epoch);
                        }
                        return Ok(EndReason::PrivacyClosed);
                    }
                    supersede_speech(
                        channels,
                        speaker,
                        &mut speech,
                        snapshot,
                        output.privacy,
                        flush_count,
                    )?;
                }
                Control::ConnectReference { peer } => {
                    if link.is_some() {
                        return Err(io::Error::other("reference peer already bound").into());
                    }
                    link = Some(ReferenceChannels::bind(directory, "speaker", worker, peer)?);
                }
                Control::Stop => {
                    speaker.stop()?;
                    report_speech_reset(channels, &mut speech)?;
                    diagnostic_reset(
                        recording,
                        ResetReason::Stop,
                        0,
                        0,
                        speaker.queued_range().epoch,
                        output.privacy.unwrap_or(0),
                        speaker.last_discard(),
                    );
                    if let Some(link) = link.as_mut() {
                        link.notify_stopped_after_local_close(speaker.queued_range().epoch);
                    }
                    return Ok(EndReason::Stopped);
                }
                Control::StartCapture => {
                    return Err(io::Error::other("capture command sent to speaker").into());
                }
            }
        }
        speaker.maintain()?;
        if report_discard(channels, speaker, &mut output, &mut flush_count, recording)? {
            report_speech_reset(channels, &mut speech)?;
        }
        if let Some(tail) = speaker.take_retired_tail()? {
            channels.control.send(WorkerEvent::CancelledTailRetired {
                owner: tail.owner,
                superseded_by: tail.superseded_by,
                first_sample: tail.first_sample,
                end_sample: tail.end_sample,
                queued_frames: tail.queued_frames,
                speech_frames: tail.speech_frames,
                started_at_us: tail.started_at_us,
                observed_at_us: speaker
                    .last_poll()
                    .ok_or_else(|| {
                        io::Error::other("tail retirement requires a device observation")
                    })?
                    .observed_at_us,
            })?;
        }
        // A hard deadline can expire while collecting the retirement receipt.
        if report_discard(channels, speaker, &mut output, &mut flush_count, recording)? {
            report_speech_reset(channels, &mut speech)?;
        }
        let Some(link) = link.as_mut() else {
            finish_tick(tick);
            continue;
        };
        if !link.connect_step()? {
            finish_tick(tick);
            continue;
        }
        for index in 0..=MAX_CONTROL {
            let Some(control) = link.control.receive::<ReferenceControl>()? else {
                break;
            };
            if index == MAX_CONTROL {
                return Err(io::Error::other("speaker reference control tick bound").into());
            }
            match control {
                ReferenceControl::CapturePrepared { privacy_generation }
                    if output.privacy.is_none() =>
                {
                    output.privacy = Some(privacy_generation);
                }
                ReferenceControl::ReferencePrimed {
                    playback_epoch,
                    analysed_through,
                } if output.waiting_ack
                    && playback_epoch == speaker.queued_range().epoch
                    && analysed_through == PRIME_FRAMES as u64 =>
                {
                    let token = speaker.zero_authority()?;
                    // A host bound on the method call, not exact DAC start.
                    // Failed/expired starts return before any success receipt.
                    let start_requested_at_us = monotonic_us();
                    let started_at_us = speaker.start_clock(token)?;
                    output.waiting_ack = false;
                    output.primed_at = None;
                    link.control.send(ReferenceControl::ClockStarted {
                        playback_epoch,
                        start_requested_at_us,
                        started_at_us,
                    })?;
                }
                _ => {
                    return Err(output
                        .fault(speaker, ReferenceFaultKind::InvalidStart)
                        .into());
                }
            }
        }
        if output
            .primed_at
            .is_some_and(|at| monotonic_us().saturating_sub(at) >= BARRIER_US)
        {
            return Err(output
                .fault(speaker, ReferenceFaultKind::BarrierTimeout)
                .into());
        }
        if output.privacy.is_some() && !speaker.clock_started() {
            output.prime(speaker, link, recording)?;
        }
        if !speaker.clock_started() {
            finish_tick(tick);
            continue;
        }
        if let Some(final_write) = speech.take_retired(speaker.queued_range()) {
            channels.control.send(WorkerEvent::SpeechRetired {
                owner: final_write.owner,
                sequence: final_write.sequence,
                final_sample: final_write.cursor,
                observed_at_us: speaker
                    .last_poll()
                    .map_or_else(monotonic_us, |s| s.observed_at_us),
            })?;
        }
        if pending.is_none()
            && let Some(chunk) = channels.data.receive::<SpeakerChunk>()?
        {
            if chunk.samples.is_empty()
                || chunk.samples.len() > RENDER_SAMPLES
                || chunk.chunk_sequence <= last_sequence
                || (chunk.end_of_speech && chunk.samples.len() != RENDER_SAMPLES)
            {
                return Err(
                    io::Error::other("invalid speaker chunk size/sequence/final block").into(),
                );
            }
            last_sequence = chunk.chunk_sequence;
            match speaker.install(chunk.snapshot) {
                Ok(()) => supersede_speech(
                    channels,
                    speaker,
                    &mut speech,
                    chunk.snapshot,
                    output.privacy,
                    flush_count,
                )?,
                Err(error) if old_interaction_error(error.as_ref()) => {}
                Err(error) => return Err(error),
            }
            if report_discard(channels, speaker, &mut output, &mut flush_count, recording)? {
                report_speech_reset(channels, &mut speech)?;
            }
            pending = Some(PendingChunk { chunk, offset: 0 });
        }
        // Aim for 10..20 ms. Provider pacing must not stop the physical clock:
        // accept real zero-only PCM if speech is unavailable, and report that
        // delivery gap without consuming or completing any missing speech.
        let writable_block = speaker
            .last_poll()
            .is_some_and(|status| status.tracked_authorized_frames <= RENDER_SAMPLES);
        if speaker.clock_started() && writable_block {
            let mut wrote_speech = false;
            if let Some(partial) = pending.as_mut() {
                let result = speaker.try_write(
                    partial.chunk.permit,
                    &partial.chunk.samples[partial.offset..],
                );
                // Record actual acceptance under its original epoch even if the
                // post-write permission check already dropped it. The following
                // reset receipt marks that tail; it never reaches new-epoch AEC.
                if let Ok(written) = &result
                    && written.frames > 0
                    && let Some(recording) = recording
                {
                    let end = partial
                        .offset
                        .checked_add(written.frames)
                        .filter(|end| *end <= partial.chunk.samples.len())
                        .ok_or_else(|| {
                            io::Error::other("invalid diagnostic accepted speaker count")
                        })?;
                    let meta = recording.render(
                        *written,
                        Some(partial.chunk.permit.owner()),
                        Some(partial.chunk.chunk_sequence),
                    );
                    let _ = recording
                        .recorder
                        .try_render(meta, &partial.chunk.samples[partial.offset..end]);
                }
                if report_discard(channels, speaker, &mut output, &mut flush_count, recording)? {
                    report_speech_reset(channels, &mut speech)?;
                }
                match result {
                    Ok(written) => {
                        if !accepted_write_survived(
                            written.first_sample,
                            written.end_sample,
                            speaker.queued_range(),
                            written.frames,
                        )? {
                            // Receipt first; the dropped tail is never new-epoch render.
                            pending = None;
                            finish_tick(tick);
                            continue;
                        }
                        if written.frames > 0 {
                            wrote_speech = true;
                            let end = partial
                                .offset
                                .checked_add(written.frames)
                                .filter(|end| *end <= partial.chunk.samples.len())
                                .ok_or_else(|| {
                                    io::Error::other("invalid accepted speaker count")
                                })?;
                            let owner = partial.chunk.permit.owner();
                            output.send(
                                link,
                                written,
                                RenderPayload::Speech {
                                    owner,
                                    samples: partial.chunk.samples[partial.offset..end].to_vec(),
                                },
                            )?;
                            partial.offset = end;
                            if let Some(gap) = speech.speech_accepted(
                                owner,
                                partial.chunk.chunk_sequence,
                                written.first_sample,
                                written.end_sample,
                                written.completed_at_us,
                                partial.chunk.end_of_speech
                                    && partial.offset == partial.chunk.samples.len(),
                            )? {
                                channels.control.send(gap)?;
                            }
                            channels.control.send(WorkerEvent::Playback {
                                turn: owner.turn(),
                                sequence: partial.chunk.chunk_sequence,
                                queued_frames: written.queued_frames as usize,
                                accepted_frames: written.frames,
                                observed_at_us: written.completed_at_us,
                            })?;
                            if partial.offset == partial.chunk.samples.len() {
                                pending = None;
                            }
                        }
                    }
                    Err(error) if old_interaction_error(error.as_ref()) => {
                        channels.control.send(WorkerEvent::PlaybackRejected {
                            turn: partial.chunk.permit.owner().turn(),
                            sequence: partial.chunk.chunk_sequence,
                            reason: error.to_string(),
                        })?;
                        pending = None;
                    }
                    Err(error) => return Err(error),
                }
            }
            // A stale independently queued packet is rejected above without
            // sacrificing this tick's zero reference. A valid successor waits
            // while the immutable old tail retires; it cannot extend that tail.
            if speaker.clock_started()
                && !wrote_speech
                && (pending.is_none() || speaker.is_retiring_tail())
            {
                let token = speaker.zero_authority()?;
                let written = speaker.try_write_silence(token, RENDER_SAMPLES)?;
                if written.frames > 0 {
                    if let Some(recording) = recording {
                        let meta = recording.render(written, None, None);
                        let _ = recording.recorder.try_silence(
                            meta,
                            if speech.unfinished_owner().is_some() {
                                diagnostics::SilenceKind::SpeechGap
                            } else {
                                diagnostics::SilenceKind::Idle
                            },
                            written.frames as u16,
                        );
                    }
                    if !accepted_write_survived(
                        written.first_sample,
                        written.end_sample,
                        speaker.queued_range(),
                        written.frames,
                    )? {
                        return Err(output
                            .fault(speaker, ReferenceFaultKind::ClockStopped)
                            .into());
                    }
                    output.send(
                        link,
                        written,
                        RenderPayload::Silence {
                            frames: written.frames as u16,
                        },
                    )?;
                    if let Some(gap) = speech.zeros_accepted(
                        written.first_sample,
                        written.end_sample,
                        written.completed_at_us,
                    )? {
                        channels.control.send(gap)?;
                    }
                }
            }
        }
        finish_tick(tick);
    }
}

fn report_speech_reset(channels: &mut WorkerChannels, speech: &mut SpeechPlayback) -> Result<()> {
    if let Some(gap) = speech.reset(monotonic_us()) {
        channels.control.send(gap)?;
    }
    Ok(())
}

fn supersede_speech(
    channels: &mut WorkerChannels,
    speaker: &mut Speaker,
    speech: &mut SpeechPlayback,
    snapshot: lamp_interaction::Snapshot,
    microphone_generation: Option<u64>,
    previous_flush_count: u64,
) -> Result<()> {
    if speech
        .owner()
        .is_some_and(|owner| snapshot.owner() != Some(owner))
        && speaker.flush_count() == previous_flush_count
    {
        if !speech.can_supersede(
            snapshot,
            microphone_generation,
            MonoTime::from_micros(monotonic_us()),
        ) {
            speaker.stop()?;
        }
        report_speech_reset(channels, speech)?;
    }
    Ok(())
}

fn report_discard(
    channels: &mut WorkerChannels,
    speaker: &mut Speaker,
    output: &mut ReferenceOutput,
    flush_count: &mut u64,
    recording: &mut Option<Recording>,
) -> Result<bool> {
    if speaker.flush_count() == *flush_count {
        return Ok(false);
    }
    *flush_count = speaker.flush_count();
    diagnostic_reset(
        recording,
        ResetReason::PlaybackReset,
        0,
        0,
        speaker.queued_range().epoch,
        output.privacy.unwrap_or(0),
        speaker.last_discard(),
    );
    if let Some(discarded) = speaker.take_discard() {
        channels.control.send(WorkerEvent::PlaybackDiscarded {
            owner: discarded.owner,
            reason: discarded.reason.into(),
            queued_frames: discarded.queued_frames,
            other_owners: discarded.other_owners,
            observed_at_us: monotonic_us(),
        })?;
    }
    output.reset();
    Ok(true)
}
fn report_fault(
    channels: &mut WorkerChannels,
    error: &(dyn std::error::Error + Send + Sync + 'static),
) {
    if let Some(fault) = error.downcast_ref::<ReferenceFault>() {
        let _ = channels
            .control
            .send(WorkerEvent::ReferenceFault { fault: *fault });
    } else {
        let _ = channels.control.send(WorkerEvent::Fault {
            code: error.to_string(),
        });
    }
}
fn rejected_old_state(error: InteractionError) -> bool {
    matches!(
        error,
        InteractionError::StaleSnapshot
            | InteractionError::WrongBoot
            | InteractionError::FutureState
            | InteractionError::ExpiredState
    )
}
fn old_interaction_error(error: &(dyn std::error::Error + Send + Sync + 'static)) -> bool {
    error
        .downcast_ref::<InteractionError>()
        .is_some_and(|error| {
            rejected_old_state(*error)
                || matches!(
                    error,
                    InteractionError::PrivacyClosed
                        | InteractionError::NoAuthority
                        | InteractionError::ExpiredPermit
                        | InteractionError::FuturePermit
                        | InteractionError::StaleOwner
                        | InteractionError::InputStillActive
                        | InteractionError::StaleCameraGrant
                        | InteractionError::StalePresentation
                        | InteractionError::StateTooOld
                )
        })
}
fn finish_tick(started: Instant) {
    if let Some(remaining) = TICK.checked_sub(started.elapsed()) {
        std::thread::sleep(remaining);
    }
}
