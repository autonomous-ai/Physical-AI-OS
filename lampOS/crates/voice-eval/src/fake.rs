//! Deterministic fake runtime for offline scenarios.
//!
//! It mirrors the turn policy of `lamp-live`'s directed coordinator (source
//! 64529dee): lamp-live's own `TurnDetector` decides admission and endpoints
//! from 10 ms blocks; every admission cancels the current reply first
//! (`user_interrupted`); the endpoint sends the input to the provider; a reply
//! completes only after its final speech sample retires. There is no addressee,
//! acknowledgment or echo gate, because the runtime has none yet.
//!
//! Speech probability comes either from declared stimulus intervals (timing
//! mode) or from lamp-live's VAD on the exact cached digital mix (signal mode).
//! Neither includes room acoustics, the loudspeaker, the microphone or AEC, and
//! host VAD numerics can differ from native ARM64. Results describe the policy,
//! not the physical Lamp.
use crate::{
    Result, Rng,
    events::{ClockDomain, EventKind, EventSource, RuntimeEvent},
    invalid,
    plan::{
        EchoModel, FakeProfile, FakeReply, FixedReply, ProviderFault, ProviderKind, Scenario, Step,
    },
    record::{ClockMapping, Stratum},
    runner::{AttemptContext, Backend, Begin, Finished, Injection},
    stimulus::{AssetIndex, SceneTiming, StimulusCatalog},
};
use lamp_live::activity::{Activity, ObservedAudio, SpeechProbability, TurnDetector};
use serde_json::{Value, json};
use std::{collections::VecDeque, sync::Arc};

pub const TICK_US: u64 = 10_000;
const SAMPLES: usize = lamp_audio::CAPTURE_SAMPLES;

struct Source {
    start_us: u64,
    timing: SceneTiming,
    pcm: Option<Arc<Vec<i16>>>,
}

#[derive(Debug)]
struct Reply {
    turn: u64,
    prefix_start_us: u64,
    endpoint_us: Option<u64>,
    first_audio_at: Option<u64>,
    first_audio_seen: bool,
    fail_at: Option<u64>,
    total_us: u64,
    text: String,
    generated_us: u64,
    played_us: u64,
    gap_until: Option<u64>,
    gap_used: bool,
    first_write_us: Option<u64>,
    in_gap: bool,
    gaps: u64,
    generation_done: bool,
    retire_at: Option<u64>,
}

/// One isolated simulated session.
pub struct FakeRuntime {
    profile: FakeProfile,
    provider: ProviderKind,
    fixed_reply: FixedReply,
    replies: Vec<FakeReply>,
    faults: Vec<ProviderFault>,
    rng: Rng,
    now_us: u64,
    ready_at_us: u64,
    ready: bool,
    session_end_us: u64,
    ended: bool,
    sequence: u64,
    detector: TurnDetector,
    vad: Option<SpeechProbability>,
    sources: Vec<Source>,
    turn: u64,
    candidates: u64,
    answers: usize,
    first_turn: Option<u64>,
    reply: Option<Reply>,
    tails: Vec<(u64, u64)>,
    leaks: Vec<(u64, u64)>,
    out: VecDeque<RuntimeEvent>,
}

impl FakeRuntime {
    pub fn new(
        profile: FakeProfile,
        scenario: &Scenario,
        fixed_reply: FixedReply,
        signal: bool,
        seed: u64,
    ) -> Self {
        let mut runtime = Self {
            ready_at_us: u64::from(profile.startup_ms) * 1000,
            session_end_us: u64::MAX,
            profile,
            provider: scenario.provider,
            fixed_reply,
            replies: scenario.fake_replies.clone(),
            faults: scenario.fake_faults.clone(),
            rng: Rng::new(seed),
            now_us: 0,
            ready: false,
            ended: false,
            sequence: 0,
            detector: TurnDetector::default(),
            vad: signal.then(SpeechProbability::default),
            sources: Vec::new(),
            turn: 0,
            candidates: 0,
            answers: 0,
            first_turn: None,
            reply: None,
            tails: Vec::new(),
            leaks: Vec::new(),
            out: VecDeque::new(),
        };
        runtime.session_end_us =
            runtime.ready_at_us + u64::from(scenario.session_seconds) * 1_000_000;
        let provider_kind = match scenario.provider {
            ProviderKind::Gemini => "fake_gemini_like",
            ProviderKind::FixedReply => "one_cached_reply",
        };
        runtime.emit(
            EventKind::RunStart {
                provider_kind: Some(provider_kind.into()),
            },
            None,
        );
        runtime
    }

    pub fn now_us(&self) -> u64 {
        self.now_us
    }
    pub fn ended(&self) -> bool {
        self.ended
    }
    pub fn pop(&mut self) -> Option<RuntimeEvent> {
        self.out.pop_front()
    }

    fn emit(&mut self, kind: EventKind, turn: Option<u64>) {
        self.out.push_back(RuntimeEvent::new(
            kind,
            turn,
            self.now_us,
            ClockDomain::Virtual,
            EventSource::Fake,
        ));
    }

    /// A ring cue request, as the choreography policy derives it from the
    /// turn's state (listening, waiting or speaking).
    fn ring(&mut self, phase: &str, turn: u64) {
        if self.profile.ring_cues {
            self.emit(
                EventKind::RingRequested {
                    phase: phase.into(),
                },
                Some(turn),
            );
        }
    }

    /// Start a stimulus at `at_us` (or now, if that has passed).
    pub fn inject(&mut self, timing: SceneTiming, pcm: Option<Arc<Vec<i16>>>, at_us: u64) -> u64 {
        let start_us = at_us.max(self.now_us);
        self.sources.push(Source {
            start_us,
            timing,
            pcm,
        });
        start_us
    }

    fn timing_probability(&self, block_mid: u64) -> f32 {
        let speaking = self.sources.iter().any(|source| {
            source.timing.clips.iter().any(|clip| {
                let start = source.start_us + u64::from(clip.start_ms) * 1000;
                let end = source.start_us + u64::from(clip.end_ms) * 1000;
                (start..end).contains(&block_mid)
            })
        });
        if speaking {
            self.profile.timing_speech_probability
        } else {
            self.profile.timing_silence_probability
        }
    }

    /// The exact digital mix of all active sources for one capture block.
    fn mix_block(&self, block_start: u64) -> [i16; SAMPLES] {
        let mut sum = [0_i32; SAMPLES];
        for source in &self.sources {
            let Some(pcm) = &source.pcm else { continue };
            if block_start < source.start_us {
                // Sources start on the block grid in practice; a partial
                // first block is mixed from its first sample.
                let skip = ((source.start_us - block_start) * 16 / 1000) as usize;
                for (index, value) in sum.iter_mut().enumerate().skip(skip) {
                    if let Some(&sample) = pcm.get(index - skip) {
                        *value += i32::from(sample);
                    }
                }
                continue;
            }
            let offset = ((block_start - source.start_us) * 16 / 1000) as usize;
            for (index, value) in sum.iter_mut().enumerate() {
                if let Some(&sample) = pcm.get(offset + index) {
                    *value += i32::from(sample);
                }
            }
        }
        sum.map(|value| value.clamp(i32::from(i16::MIN), i32::from(i16::MAX)) as i16)
    }

    /// Advance one 10 ms block. Order follows the coordinator tick: speaker
    /// receipts first, then provider output, then capture and admission.
    pub fn tick(&mut self) {
        if self.ended {
            return;
        }
        self.now_us += TICK_US;
        if !self.ready {
            if self.now_us >= self.ready_at_us {
                self.ready = true;
                self.emit(EventKind::ListeningReady, None);
            }
            return;
        }
        if self.now_us >= self.session_end_us {
            if let Some(reply) = self.reply.take() {
                self.cancel(reply, "deadline_cancelled");
            }
            self.emit(
                EventKind::RunEnd {
                    status: Some("completed_unscored".into()),
                    error: None,
                },
                None,
            );
            self.ended = true;
            return;
        }
        self.speaker_step();
        self.provider_step();
        if self.ended {
            return;
        }
        let block_start = self.now_us - TICK_US;
        let mid = block_start + TICK_US / 2;
        let samples = if self.vad.is_some() {
            self.mix_block(block_start)
        } else {
            [0; SAMPLES]
        };
        let mut probability = match self.vad.as_mut() {
            Some(vad) => vad.analyze(&samples),
            None => self.timing_probability(mid),
        };
        if self
            .leaks
            .iter()
            .any(|&(from, to)| (from..to).contains(&mid))
        {
            probability = probability.max(0.95);
        }
        self.sequence += 1;
        let frame = ObservedAudio {
            sequence: self.sequence,
            captured_at_us: self.now_us,
            samples,
        };
        match self.detector.push(frame, probability) {
            Activity::Quiet | Activity::Continue(_) => {}
            Activity::Start(prefix) => {
                let prefix_start = prefix.first().map_or(self.now_us, |p| p.captured_at_us);
                self.admit(prefix_start);
            }
            Activity::End(frame) => self.endpoint(frame.captured_at_us),
            Activity::Fault(reason) => {
                self.fail(reason);
                return;
            }
        }
        let now = self.now_us;
        let due: Vec<u64> = self
            .tails
            .iter()
            .filter(|(at, _)| *at <= now)
            .map(|(_, t)| *t)
            .collect();
        self.tails.retain(|(at, _)| *at > now);
        for turn in due {
            self.emit(EventKind::CancelledTailRetired, Some(turn));
        }
    }

    /// Mirrors lamp-live since 9c2c82e1: a candidate is recorded, directed
    /// mode accepts it at once on VAD alone, the old reply is revoked, and
    /// only then is the new owner admitted.
    fn admit(&mut self, prefix_start_us: u64) {
        self.candidates += 1;
        let candidate = json!({"serial": self.candidates});
        self.emit(
            EventKind::InputCandidate {
                candidate: candidate.clone(),
            },
            None,
        );
        if let Some(reply) = self.reply.take() {
            self.cancel(reply, "user_interrupted");
        }
        self.turn += 1;
        self.first_turn.get_or_insert(self.turn);
        self.emit(
            EventKind::InputAdmitted {
                prefix_first_read_us: Some(prefix_start_us),
                candidate: Some(candidate),
                basis: Some("directed_session_vad_only".into()),
            },
            Some(self.turn),
        );
        self.emit(EventKind::ProviderInputStarted, Some(self.turn));
        self.ring("listening", self.turn);
        self.reply = Some(Reply {
            turn: self.turn,
            prefix_start_us,
            endpoint_us: None,
            first_audio_at: None,
            first_audio_seen: false,
            fail_at: None,
            total_us: 0,
            text: String::new(),
            generated_us: 0,
            played_us: 0,
            gap_until: None,
            gap_used: false,
            first_write_us: None,
            in_gap: false,
            gaps: 0,
            generation_done: false,
            retire_at: None,
        });
    }

    fn fault(&self, turn: u64) -> Option<&ProviderFault> {
        self.faults.iter().find(|fault| fault.turn() == turn)
    }

    /// Words of the stimuli heard between the retained prefix and the endpoint,
    /// with speech before the prefix dropped proportionally. A simulated
    /// transcript for review, not ASR evidence.
    fn heard_text(&self, from_us: u64, to_us: u64) -> String {
        let mut words = Vec::new();
        for source in &self.sources {
            for clip in &source.timing.clips {
                let start = source.start_us + u64::from(clip.start_ms) * 1000;
                let end = source.start_us + u64::from(clip.end_ms) * 1000;
                if end <= from_us || start >= to_us {
                    continue;
                }
                let all: Vec<&str> = clip.text.split_whitespace().collect();
                let lost = from_us.saturating_sub(start) as f64 / (end - start).max(1) as f64;
                let skip = (lost * all.len() as f64).ceil() as usize;
                words.extend(all.into_iter().skip(skip));
            }
        }
        words.join(" ")
    }

    fn endpoint(&mut self, last_block_us: u64) {
        let Some(mut reply) = self.reply.take() else {
            self.fail("speech end lost its owner");
            return;
        };
        let now = self.now_us;
        reply.endpoint_us = Some(now);
        self.ring("waiting", reply.turn);
        self.emit(
            EventKind::LocalEndpoint {
                last_block_read_us: Some(last_block_us),
            },
            Some(reply.turn),
        );
        let heard = self.heard_text(reply.prefix_start_us.saturating_sub(TICK_US), now);
        self.emit(
            EventKind::InputTranscript {
                text: heard,
                finished: true,
            },
            None,
        );
        match self.provider {
            ProviderKind::FixedReply => {
                // lamp-live's fixture provider reserves its single reply for the
                // first admitted owner; later turns complete without audio.
                if self.first_turn == Some(reply.turn) {
                    reply.total_us = u64::from(self.fixed_reply.duration_ms()) * 1000;
                    reply.text = self.fixed_reply.text.clone();
                    reply.first_audio_at =
                        Some(now + u64::from(self.profile.fixed_reply_first_audio_ms) * 1000);
                } else {
                    reply.generation_done = true;
                }
            }
            ProviderKind::Gemini => {
                let script = self
                    .replies
                    .get(self.answers)
                    .cloned()
                    .unwrap_or(FakeReply {
                        text: "Okay.".into(),
                        speech_ms: 800,
                    });
                self.answers += 1;
                reply.total_us = u64::from(script.speech_ms) * 1000;
                reply.text = script.text;
                let jitter = self
                    .rng
                    .below_inclusive(u64::from(self.profile.first_audio_jitter_ms));
                let mut delay = u64::from(self.profile.first_audio_ms) + jitter;
                match self.fault(reply.turn) {
                    Some(ProviderFault::FirstAudioDelay { delay_ms, .. }) => {
                        delay = u64::from(*delay_ms)
                    }
                    Some(ProviderFault::FailBeforeAudio {
                        after_endpoint_ms, ..
                    }) => {
                        reply.fail_at = Some(now + u64::from(*after_endpoint_ms) * 1000);
                    }
                    _ => {}
                }
                reply.first_audio_at = Some(now + delay * 1000);
            }
        }
        self.reply = Some(reply);
    }

    fn cancel(&mut self, reply: Reply, reason: &str) {
        self.emit(
            EventKind::TurnCancelled {
                reason: Some(reason.into()),
                provider_audio_seen: Some(reply.first_audio_seen),
            },
            Some(reply.turn),
        );
        let playing =
            reply.first_write_us.is_some() && reply.retire_at.is_none_or(|at| at > self.now_us);
        match reason {
            // A successor's admission retires the old queued tail.
            "user_interrupted" if playing => self.tails.push((
                self.now_us + u64::from(self.profile.cancel_tail_ms) * 1000,
                reply.turn,
            )),
            // Other revocations report a stale-owner discard that the
            // coordinator classifies as expected cancellation.
            "provider_interrupted" | "deadline_cancelled" if playing => {
                self.emit(
                    EventKind::PlaybackDiscarded { expected: true },
                    Some(reply.turn),
                );
            }
            _ => {}
        }
    }

    /// Provider/worker failure: lamp-live cancels with `runtime_failed` and the
    /// whole finite run fails. It has no spoken failure path yet.
    fn fail(&mut self, error: &str) {
        if let Some(reply) = self.reply.take() {
            self.cancel(reply, "runtime_failed");
        }
        self.emit(
            EventKind::RunEnd {
                status: Some("failed".into()),
                error: Some(error.into()),
            },
            None,
        );
        self.ended = true;
    }

    fn provider_step(&mut self) {
        let now = self.now_us;
        let speed = u64::from(self.profile.generation_speed_percent);
        let burst = u64::from(self.profile.first_burst_ms) * 1000;
        let Some(reply) = self.reply.as_mut() else {
            return;
        };
        if reply.fail_at.is_some_and(|at| now >= at) && !reply.first_audio_seen {
            self.fail("provider failed before audio (fault injection)");
            return;
        }
        let turn = reply.turn;
        if reply.endpoint_us.is_some() && reply.total_us == 0 && reply.generation_done {
            // A turn the provider completes without audio.
            self.emit(EventKind::ProviderTurnComplete, Some(turn));
            self.emit(
                EventKind::TurnCompleted {
                    outcome: "no_audio_answer".into(),
                    playback_gaps: 0,
                },
                Some(turn),
            );
            self.reply = None;
            return;
        }
        let mut events = Vec::new();
        if !reply.first_audio_seen && reply.first_audio_at.is_some_and(|at| now >= at) {
            reply.first_audio_seen = true;
            reply.generated_us = burst.min(reply.total_us);
            events.push(EventKind::ProviderFirstAudio);
        } else if reply.first_audio_seen
            && !reply.generation_done
            && reply.gap_until.is_none_or(|until| now >= until)
        {
            reply.generated_us = (reply.generated_us + TICK_US * speed / 100).min(reply.total_us);
        }
        let fault = self.faults.iter().find(|f| f.turn() == turn).cloned();
        let reply = self.reply.as_mut().expect("reply checked above");
        match fault {
            Some(ProviderFault::SupplyGap {
                after_audio_ms,
                gap_ms,
                ..
            }) if !reply.gap_used && reply.generated_us >= u64::from(after_audio_ms) * 1000 => {
                reply.gap_used = true;
                reply.gap_until = Some(now + u64::from(gap_ms) * 1000);
            }
            Some(ProviderFault::DisconnectMidReply { after_audio_ms, .. })
                if reply.first_audio_seen
                    && reply.generated_us >= u64::from(after_audio_ms) * 1000 =>
            {
                for kind in events {
                    self.emit(kind, Some(turn));
                }
                self.fail("provider disconnected mid-reply (fault injection)");
                return;
            }
            _ => {}
        }
        if reply.first_audio_seen && !reply.generation_done && reply.generated_us >= reply.total_us
        {
            reply.generation_done = true;
            events.push(EventKind::OutputTranscript {
                text: reply.text.clone(),
                finished: true,
            });
            events.push(EventKind::ProviderTurnComplete);
        }
        for kind in events {
            self.emit(kind, Some(turn));
        }
    }

    fn speaker_step(&mut self) {
        let now = self.now_us;
        let dispatch = u64::from(self.profile.speaker_dispatch_ms) * 1000;
        let alsa = u64::from(self.profile.alsa_delay_ms) * 1000;
        let echo = self.profile.echo.clone();
        let Some(reply) = self.reply.as_mut() else {
            return;
        };
        let turn = reply.turn;
        let mut events = Vec::new();
        if reply.first_write_us.is_none()
            && reply.first_audio_seen
            && reply.first_audio_at.is_some_and(|at| now >= at + dispatch)
        {
            reply.first_write_us = Some(now);
            events.push(EventKind::SpeakerFirstWrite);
            if let EchoModel::Leak {
                after_first_write_ms,
                duration_ms,
                every_reply,
            } = echo
                && (every_reply || self.leaks.is_empty())
            {
                let from = now + u64::from(after_first_write_ms) * 1000;
                self.leaks
                    .push((from, from + u64::from(duration_ms) * 1000));
            }
        } else if reply.first_write_us.is_some() && reply.retire_at.is_none() {
            let available = reply.generated_us.saturating_sub(reply.played_us);
            if available >= TICK_US || (reply.generation_done && available > 0) {
                reply.played_us += available.min(TICK_US);
                if reply.in_gap {
                    reply.in_gap = false;
                    events.push(EventKind::PlaybackGap {
                        phase: "resumed".into(),
                    });
                }
            } else if !reply.generation_done && !reply.in_gap {
                reply.in_gap = true;
                reply.gaps += 1;
                events.push(EventKind::PlaybackGap {
                    phase: "started".into(),
                });
            }
            if reply.generation_done && reply.played_us >= reply.total_us {
                reply.retire_at = Some(now + alsa);
            }
        }
        let spurious = self.faults.iter().find_map(|fault| match fault {
            ProviderFault::SpuriousInterrupt {
                turn: t,
                after_first_write_ms,
            } if *t == turn => Some(u64::from(*after_first_write_ms) * 1000),
            _ => None,
        });
        let reply = self.reply.as_mut().expect("reply checked above");
        let interrupt = spurious.is_some_and(|after| {
            reply.retire_at.is_none() && reply.first_write_us.is_some_and(|at| now >= at + after)
        });
        let retired = reply.retire_at.is_some_and(|at| now >= at);
        let gaps = reply.gaps;
        let started = events.contains(&EventKind::SpeakerFirstWrite);
        for kind in events {
            self.emit(kind, Some(turn));
        }
        if started {
            self.ring("speaking", turn);
        }
        if interrupt {
            self.emit(EventKind::ProviderInterrupted, Some(turn));
            let reply = self.reply.take().expect("reply checked above");
            self.cancel(reply, "provider_interrupted");
            // The coordinator restarts turn detection after a provider interruption.
            self.detector = TurnDetector::default();
        } else if retired {
            self.emit(EventKind::SpeechRetired, Some(turn));
            let outcome = if gaps > 0 {
                "audio_written_with_playback_gaps_unscored"
            } else {
                "audio_written_unscored"
            };
            self.emit(
                EventKind::TurnCompleted {
                    outcome: outcome.into(),
                    playback_gaps: gaps,
                },
                Some(turn),
            );
            self.reply = None;
        }
    }
}

/// Virtual-time backend for `run_suite`.
pub struct FakeBackend<'a> {
    profile_id: String,
    profile: FakeProfile,
    fixed_reply: FixedReply,
    assets: Option<&'a AssetIndex>,
    runtime: Option<FakeRuntime>,
    events: Vec<RuntimeEvent>,
}

impl<'a> FakeBackend<'a> {
    pub fn new(
        profile_id: &str,
        profile: FakeProfile,
        fixed_reply: FixedReply,
        assets: Option<&'a AssetIndex>,
    ) -> Self {
        Self {
            profile_id: profile_id.into(),
            profile,
            fixed_reply,
            assets,
            runtime: None,
            events: Vec::new(),
        }
    }
    fn runtime(&mut self) -> Result<&mut FakeRuntime> {
        self.runtime
            .as_mut()
            .ok_or_else(|| invalid("fake session not started"))
    }
}

impl Backend for FakeBackend<'_> {
    fn describe(&self) -> Value {
        json!({
            "kind": "fake",
            "profile": self.profile_id,
            "profile_parameters": self.profile,
            "speech_probability": if self.assets.is_some() {"lamp-live VAD on cached digital mixes"} else {"declared stimulus intervals"},
            "policy_source": "lamp-live directed coordinator at 64529dee, re-implemented; detector is lamp_live::activity::TurnDetector",
            "host_numerics": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
        })
    }
    fn stratum(&self, _: &Scenario) -> Stratum {
        if self.assets.is_some() {
            Stratum::FakeSignal
        } else {
            Stratum::FakeTiming
        }
    }
    fn domain(&self) -> ClockDomain {
        ClockDomain::Virtual
    }
    fn profile(&self) -> Option<String> {
        Some(self.profile_id.clone())
    }
    fn unsupported(&self, scenario: &Scenario) -> Option<String> {
        let assets = self.assets?;
        scenario
            .steps
            .iter()
            .filter_map(|step| step.scene.as_deref())
            .find(|scene| assets.get(scene).is_none())
            .map(|scene| {
                format!(
                    "signal mode needs a verified cached mix for scene {scene}; none is rendered"
                )
            })
    }
    fn begin(&mut self, context: &AttemptContext) -> Result<Begin> {
        self.runtime = Some(FakeRuntime::new(
            self.profile.clone(),
            context.scenario,
            self.fixed_reply.clone(),
            self.assets.is_some(),
            context.seed,
        ));
        self.events.clear();
        Ok(Begin::Ready)
    }
    fn now_us(&mut self) -> u64 {
        self.runtime.as_ref().map_or(0, FakeRuntime::now_us)
    }
    fn next_event(&mut self, until_us: u64) -> Result<Option<RuntimeEvent>> {
        let runtime = self.runtime()?;
        loop {
            if let Some(event) = runtime.pop() {
                let mut event = event;
                event.received_us = event.at_us;
                self.events.push(event.clone());
                return Ok(Some(event));
            }
            if runtime.ended() || runtime.now_us() >= until_us {
                return Ok(None);
            }
            runtime.tick();
        }
    }
    fn runner_time(&self, event: &RuntimeEvent) -> (u64, String) {
        (event.at_us.unwrap_or(0), "virtual clock".into())
    }
    fn timing(&mut self, catalog: &StimulusCatalog, scene: &str) -> Result<SceneTiming> {
        let mut timing = catalog.estimate(scene)?;
        if let Some(assets) = self.assets {
            timing.measure(&assets.pcm(scene)?);
        }
        Ok(timing)
    }
    fn inject(
        &mut self,
        _: &Step,
        scene: &str,
        timing: &SceneTiming,
        at_us: u64,
    ) -> Result<Injection> {
        let pcm = match self.assets {
            Some(assets) => Some(Arc::new(assets.pcm(scene)?)),
            None => None,
        };
        let started_us = self.runtime()?.inject(timing.clone(), pcm, at_us);
        Ok(Injection {
            started_us,
            receipt: json!({"virtual_start_us": started_us, "late_us": started_us - at_us}),
        })
    }
    fn finish(&mut self, _: &AttemptContext) -> Result<Finished> {
        // Run the finite session to its declared end, like lamp-live.
        while self.next_event(u64::MAX)?.is_some() {}
        Ok(Finished {
            events: std::mem::take(&mut self.events),
            clock: Some(ClockMapping::identity(ClockDomain::Virtual)),
            evidence: json!({"room_audio": null, "note": "fake runtime: no audio was played or recorded"}),
            aborted: None,
            unmapped: 0,
            spoken_failure_notice: Some(self.profile.spoken_failure_notice),
            corrected_starts: Vec::new(),
            failed_deliveries: Vec::new(),
        })
    }
    fn reproduce(&self, context: &AttemptContext) -> String {
        format!(
            "cargo run -p lamp-voice-eval -- fake-run --profile {} --scenario {} --attempt-seed {} --out NEW_DIR{}",
            self.profile_id,
            context.scenario.id,
            context.seed,
            if self.assets.is_some() {
                " --cache CACHE --manifest MANIFEST"
            } else {
                ""
            }
        )
    }
}
