//! Normalized runtime events. Every timestamp keeps its clock domain; values
//! from different domains are never subtracted without an explicit mapping.
use crate::{Result, invalid};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClockDomain {
    /// Fake runtime simulation time. Never comparable with wall clocks.
    Virtual,
    /// Lamp host `CLOCK_MONOTONIC` microseconds (lamp-live traces and cues).
    LampMonotonic,
    /// Runner host `CLOCK_MONOTONIC` microseconds (the Mac driving stimuli).
    RunnerMonotonic,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventSource {
    Fake,
    /// Final `events.jsonl` written by lamp-live.
    Trace,
    /// Live scheduling cue from lamp-live's optional cue socket.
    Cue,
    /// A status line lamp-live printed on stdout.
    Stdout,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum EventKind {
    RunStart {
        provider_kind: Option<String>,
    },
    ListeningReady,
    /// A speech candidate before any destructive turn replacement.
    InputCandidate {
        candidate: Value,
    },
    InputCandidateRejected {
        candidate: Value,
        reason: Option<String>,
    },
    InputAdmitted {
        prefix_first_read_us: Option<u64>,
        /// Candidate ID and admission basis, recorded since lamp-live 9c2c82e1.
        #[serde(default)]
        candidate: Option<Value>,
        #[serde(default)]
        basis: Option<String>,
    },
    /// A requested ring cue (`--ring-channel-ceiling`); SPI/optical output unmeasured.
    RingRequested {
        phase: String,
    },
    ProviderInputStarted,
    LocalEndpoint {
        last_block_read_us: Option<u64>,
    },
    ProviderFirstAudio,
    SpeakerFirstWrite,
    SpeechRetired,
    PlaybackGap {
        phase: String,
    },
    /// A reply revoked before completion (`turn_finished` with an owner).
    /// Cues preserve the optional original outcome; the final trace also carries it.
    TurnCancelled {
        reason: Option<String>,
        provider_audio_seen: Option<bool>,
    },
    /// Normal completion (`turn_finished` without an owner).
    TurnCompleted {
        outcome: String,
        playback_gaps: u64,
    },
    ProviderInterrupted,
    ProviderTurnComplete,
    CancelledTailRetired,
    PlaybackDiscarded {
        expected: bool,
    },
    /// Provider ASR of the person's speech; session scoped, not turn correlated.
    InputTranscript {
        text: String,
        finished: bool,
    },
    OutputTranscript {
        text: String,
        finished: bool,
    },
    RuntimeFault {
        reason: String,
    },
    RunEnd {
        status: Option<String>,
        error: Option<String>,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct RuntimeEvent {
    #[serde(flatten)]
    pub kind: EventKind,
    pub turn: Option<u64>,
    pub at_us: Option<u64>,
    pub domain: ClockDomain,
    pub source: EventSource,
    /// Receipt time on the runner, when the event arrived live.
    #[serde(default)]
    pub received_us: Option<u64>,
}

impl RuntimeEvent {
    pub fn new(
        kind: EventKind,
        turn: Option<u64>,
        at_us: u64,
        domain: ClockDomain,
        source: EventSource,
    ) -> Self {
        Self {
            kind,
            turn,
            at_us: Some(at_us),
            domain,
            source,
            received_us: None,
        }
    }
    pub fn name(&self) -> &'static str {
        match &self.kind {
            EventKind::RunStart { .. } => "run_start",
            EventKind::ListeningReady => "listening_ready",
            EventKind::InputCandidate { .. } => "input_candidate",
            EventKind::InputCandidateRejected { .. } => "input_candidate_rejected",
            EventKind::InputAdmitted { .. } => "input_admitted",
            EventKind::RingRequested { .. } => "ring_requested",
            EventKind::ProviderInputStarted => "provider_input_started",
            EventKind::LocalEndpoint { .. } => "local_endpoint",
            EventKind::ProviderFirstAudio => "provider_first_audio",
            EventKind::SpeakerFirstWrite => "speaker_first_write",
            EventKind::SpeechRetired => "speech_retired",
            EventKind::PlaybackGap { .. } => "playback_gap",
            EventKind::TurnCancelled { .. } => "turn_cancelled",
            EventKind::TurnCompleted { .. } => "turn_completed",
            EventKind::ProviderInterrupted => "provider_interrupted",
            EventKind::ProviderTurnComplete => "provider_turn_complete",
            EventKind::CancelledTailRetired => "cancelled_tail_retired",
            EventKind::PlaybackDiscarded { .. } => "playback_discarded",
            EventKind::InputTranscript { .. } => "input_transcript",
            EventKind::OutputTranscript { .. } => "output_transcript",
            EventKind::RuntimeFault { .. } => "runtime_fault",
            EventKind::RunEnd { .. } => "run_end",
        }
    }
}

fn u64_field(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

/// Map one lamp-live `events.jsonl` record. Unknown kinds return `None` and are
/// counted by the caller; they are never guessed into a known meaning.
pub fn from_trace(record: &Value) -> Option<RuntimeEvent> {
    let kind = record.get("kind")?.as_str()?;
    let turn = u64_field(record, "turn");
    let text = |key: &str| record.get(key).and_then(Value::as_str).map(str::to_owned);
    let event = match kind {
        "run_start" => EventKind::RunStart {
            provider_kind: text("provider_kind"),
        },
        "listening_ready" => EventKind::ListeningReady,
        "input_candidate" => EventKind::InputCandidate {
            candidate: record["candidate"]["id"].clone(),
        },
        "input_candidate_rejected" => EventKind::InputCandidateRejected {
            candidate: record["candidate"].clone(),
            reason: text("reason"),
        },
        "input_admitted" => EventKind::InputAdmitted {
            prefix_first_read_us: u64_field(record, "prefix_first_host_read_us"),
            candidate: record.get("candidate").filter(|c| !c.is_null()).cloned(),
            basis: text("admission_basis"),
        },
        "ring_requested" => EventKind::RingRequested {
            phase: text("phase").unwrap_or_default(),
        },
        "provider_input_started" => EventKind::ProviderInputStarted,
        "local_endpoint" => EventKind::LocalEndpoint {
            last_block_read_us: u64_field(record, "last_block_host_read_us"),
        },
        "provider_first_audio" => EventKind::ProviderFirstAudio,
        "speaker_first_write" => EventKind::SpeakerFirstWrite,
        "speech_final_sample_retired" => EventKind::SpeechRetired,
        "playback_gap" => EventKind::PlaybackGap {
            phase: record
                .get("phase")
                .map(|phase| {
                    phase
                        .as_str()
                        .map_or_else(|| phase.to_string(), str::to_owned)
                })
                .unwrap_or_default(),
        },
        // The coordinator's cancellation path records the owner; completion does not.
        "turn_finished" if record.get("owner").is_some_and(Value::is_object) => {
            EventKind::TurnCancelled {
                reason: text("outcome"),
                provider_audio_seen: record.get("provider_audio_seen").and_then(Value::as_bool),
            }
        }
        "turn_finished" => EventKind::TurnCompleted {
            outcome: text("outcome").unwrap_or_default(),
            playback_gaps: u64_field(record, "playback_gaps").unwrap_or(0),
        },
        "provider_interrupted" => EventKind::ProviderInterrupted,
        "provider_turn_complete" => EventKind::ProviderTurnComplete,
        "cancelled_tail_retired" => EventKind::CancelledTailRetired,
        "playback_discarded" => EventKind::PlaybackDiscarded {
            expected: record
                .get("expected_cancellation")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        },
        "transcript" => {
            let text = text("text").unwrap_or_default();
            let finished = record
                .get("finished")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if turn.is_some() {
                EventKind::OutputTranscript { text, finished }
            } else {
                EventKind::InputTranscript { text, finished }
            }
        }
        "reference_fault"
        | "speaker_rejected"
        | "worker_exit_failed"
        | "worker_forced_shutdown"
        | "capture_processing_mismatch"
        | "stop_delivery_error"
        | "fixture_cue_invalid" => EventKind::RuntimeFault {
            reason: kind.to_owned(),
        },
        "audio_diagnostics_certified" if record.get("valid") == Some(&Value::Bool(false)) => {
            EventKind::RuntimeFault {
                reason: "audio_diagnostics_invalid".into(),
            }
        }
        "run_end" => EventKind::RunEnd {
            status: text("status"),
            error: text("error"),
        },
        _ => return None,
    };
    // Ring requests name their turn only inside the owner.
    let turn = turn.or_else(|| record["owner"]["turn"].as_u64());
    Some(RuntimeEvent {
        kind: event,
        turn,
        at_us: u64_field(record, "at_us"),
        domain: ClockDomain::LampMonotonic,
        source: EventSource::Trace,
        received_us: None,
    })
}

/// Parse a lamp-live trace file's text, keeping a count of unmapped records.
pub fn parse_trace(text: &str) -> Result<(Vec<RuntimeEvent>, usize)> {
    let mut events = Vec::new();
    let mut unmapped = 0;
    for (index, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        if index >= 25_000 {
            return Err(invalid("trace exceeds the 20,000-event runtime bound"));
        }
        let record: Value = serde_json::from_str(line)?;
        match from_trace(&record) {
            Some(event) => events.push(event),
            None => unmapped += 1,
        }
    }
    Ok((events, unmapped))
}

/// lamp-live scheduling datagram (`fixture_provider::CueSink`, schema 1).
/// These timestamps are software events, never microphone/speaker onsets.
pub const CUE_FRESH_US: u64 = 100_000;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Cue {
    pub schema: u8,
    pub sequence: u64,
    pub kind: String,
    pub boot: [u8; 16],
    pub turn: Option<u64>,
    pub generation: Option<u64>,
    pub capture_epoch: Option<u64>,
    pub reference_epoch_context: Option<u64>,
    pub event_us: u64,
    pub sent_us: u64,
    pub expires_us: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

pub fn parse_cue(bytes: &[u8]) -> Result<Cue> {
    if bytes.len() > 512 {
        return Err(invalid("cue exceeds 512 bytes"));
    }
    let cue: Cue = serde_json::from_slice(bytes)?;
    let owned = match cue.kind.as_str() {
        "listening_ready" | "run_end" => false,
        "input_admitted"
        | "local_endpoint"
        | "speaker_first_write"
        | "speech_retired"
        | "cancelled" => true,
        _ => return Err(invalid("unknown cue kind")),
    };
    if cue.schema != 1
        || cue.sequence == 0
        || cue.boot == [0; 16]
        || cue.sent_us < cue.event_us
        || cue.sent_us >= cue.expires_us
        || cue.event_us.checked_add(CUE_FRESH_US) != Some(cue.expires_us)
        || (owned && (cue.turn.is_none_or(|v| v == 0) || cue.generation.is_none_or(|v| v == 0)))
        || (!owned && (cue.turn.is_some() || cue.generation.is_some()))
        || cue.capture_epoch == Some(0)
        || cue.reference_epoch_context == Some(0)
        || cue
            .reason
            .as_ref()
            .is_some_and(|s| cue.kind != "cancelled" || s.is_empty())
    {
        return Err(invalid("unsupported or inconsistent cue identity/times"));
    }
    Ok(cue)
}

impl Cue {
    pub fn event(&self) -> Option<RuntimeEvent> {
        let kind = match self.kind.as_str() {
            "listening_ready" => EventKind::ListeningReady,
            "input_admitted" => EventKind::InputAdmitted {
                prefix_first_read_us: None,
                candidate: None,
                basis: None,
            },
            "local_endpoint" => EventKind::LocalEndpoint {
                last_block_read_us: None,
            },
            "speaker_first_write" => EventKind::SpeakerFirstWrite,
            "speech_retired" => EventKind::SpeechRetired,
            "cancelled" => EventKind::TurnCancelled {
                reason: self.reason.clone(),
                provider_audio_seen: None,
            },
            "run_end" => EventKind::RunEnd {
                status: None,
                error: None,
            },
            _ => return None,
        };
        Some(RuntimeEvent {
            kind,
            turn: self.turn,
            at_us: Some(self.event_us),
            domain: ClockDomain::LampMonotonic,
            source: EventSource::Cue,
            received_us: None,
        })
    }
}

/// Serialize an event in lamp-live's trace vocabulary. Used by the fake
/// lamp-live process for loopback tests of the physical orchestration path.
pub fn to_trace(event: &RuntimeEvent, owner: Option<Value>) -> Value {
    let at = event.at_us.unwrap_or(0);
    let mut record = match &event.kind {
        EventKind::RunStart { provider_kind } => {
            json!({"kind":"run_start","provider_kind":provider_kind,"acoustic_score":null})
        }
        EventKind::ListeningReady => json!({"kind":"listening_ready"}),
        EventKind::InputCandidate { candidate } => {
            json!({"kind":"input_candidate","candidate":{"id":candidate}})
        }
        EventKind::InputCandidateRejected { candidate, reason } => {
            json!({"kind":"input_candidate_rejected","candidate":candidate,"reason":reason})
        }
        EventKind::InputAdmitted {
            prefix_first_read_us,
            candidate,
            basis,
        } => {
            json!({"kind":"input_admitted","owner":owner,"prefix_first_host_read_us":prefix_first_read_us,
                "candidate":candidate,"admission_basis":basis})
        }
        EventKind::RingRequested { phase } => {
            json!({"kind":"ring_requested","owner":owner,"phase":phase})
        }
        EventKind::ProviderInputStarted => {
            json!({"kind":"provider_input_started","waiting_for_barrier":false})
        }
        EventKind::LocalEndpoint { last_block_read_us } => {
            json!({"kind":"local_endpoint","owner":owner,
            "last_block_host_read_us":last_block_read_us,"includes_silence_wait_ms":600,"acoustic_speech_end":null})
        }
        EventKind::ProviderFirstAudio => json!({"kind":"provider_first_audio"}),
        EventKind::SpeakerFirstWrite => json!({"kind":"speaker_first_write","owner":owner}),
        EventKind::SpeechRetired => json!({"kind":"speech_final_sample_retired","owner":owner}),
        EventKind::PlaybackGap { phase } => json!({"kind":"playback_gap","phase":phase}),
        EventKind::TurnCancelled {
            reason,
            provider_audio_seen,
        } => json!({"kind":"turn_finished","owner":owner.unwrap_or_else(|| json!({})),
            "outcome":reason,"provider_audio_seen":provider_audio_seen,"playback_gaps":0}),
        EventKind::TurnCompleted {
            outcome,
            playback_gaps,
        } => json!({"kind":"turn_finished","outcome":outcome,"playback_gaps":playback_gaps}),
        EventKind::ProviderInterrupted => json!({"kind":"provider_interrupted"}),
        EventKind::ProviderTurnComplete => json!({"kind":"provider_turn_complete","idle":true}),
        EventKind::CancelledTailRetired => json!({"kind":"cancelled_tail_retired","owner":owner}),
        EventKind::PlaybackDiscarded { expected } => {
            json!({"kind":"playback_discarded","owner":owner,"expected_cancellation":expected})
        }
        EventKind::InputTranscript { text, finished } => {
            json!({"kind":"transcript","text":text,"finished":finished})
        }
        EventKind::OutputTranscript { text, finished } => {
            json!({"kind":"transcript","text":text,"finished":finished})
        }
        EventKind::RuntimeFault { reason } => json!({"kind":reason}),
        EventKind::RunEnd { status, error } => {
            json!({"kind":"run_end","status":status,"error":error})
        }
    };
    record["at_us"] = json!(at);
    record["turn"] = event.turn.map_or(Value::Null, |turn| json!(turn));
    record
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Records copied field-for-field from the `json!` calls in
    /// `crates/live/src/coordinator.rs` at source 64529dee.
    const COORDINATOR_RECORDS: &str = r#"{"kind":"run_start","at_us":10,"scope":"directed_voice_only","seconds":30,"acoustic_score":null,"audio_diagnostics_requested":true,"provider_kind":"one_cached_reply","fixture":null,"cue_requested":true}
{"kind":"listening_ready","at_us":20,"scope":"explicit directed session; addressee inference absent"}
{"kind":"input_admitted","owner":{"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":1,"generation":2},"turn":1,"at_us":30,"authority_issued_at_us":29,"prefix_first_host_read_us":5}
{"kind":"local_endpoint","turn":1,"at_us":40,"last_block_host_read_us":39,"includes_silence_wait_ms":600,"acoustic_speech_end":null}
{"kind":"speaker_first_write","turn":1,"owner":{"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":1,"generation":2},"at_us":50,"boundary":"ALSA accepted; acoustic onset unmeasured"}
{"kind":"turn_finished","owner":{"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":1,"generation":2},"turn":1,"at_us":60,"outcome":"user_interrupted","provider_audio_seen":true,"playback_gaps":0}
{"kind":"turn_finished","turn":2,"at_us":70,"outcome":"audio_written_with_playback_gaps_unscored","playback_gaps":1}
{"kind":"transcript","turn":null,"text":"How are you doing today?","finished":true,"at_us":80,"input_correlation":"unreliable; session scoped"}
{"kind":"transcript","turn":2,"text":"I'm doing well","finished":false,"at_us":81,"input_correlation":"output lineage"}
{"kind":"reference_fault","worker":"speaker","details":{}}
{"kind":"session_boot","at_us":1,"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]}
{"kind":"run_end","at_us":90,"status":"completed_unscored","error":null,"cue_valid":true}"#;

    /// Records from the candidate/ring vocabulary at source 23bde487.
    const CANDIDATE_RECORDS: &str = r#"{"kind":"input_candidate","candidate":{"id":{"controller":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"serial":4},"trigger_sequence":40,"trigger_read_at_us":95,"first_read_at_us":40,"decision_deadline_us":295,"displaced_owner":null},"at_us":100}
{"kind":"input_admitted","owner":{"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":2,"generation":3},"turn":2,"at_us":101,"authority_issued_at_us":100,"prefix_first_host_read_us":40,"candidate":{"controller":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"serial":4},"admission_basis":"directed_session_vad_only"}
{"kind":"input_candidate_rejected","candidate":{"controller":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"serial":5},"reason":"deadline","at_us":120}
{"kind":"ring_requested","at_us":130,"owner":{"boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":2,"generation":3},"phase":"listening","ceiling":24,"permit_expires_at_us":230}"#;

    #[test]
    fn candidate_and_ring_vocabulary_maps_with_turns_and_ids() {
        let (events, unmapped) = parse_trace(CANDIDATE_RECORDS).unwrap();
        assert_eq!(unmapped, 0);
        let controller = [1_u8; 16];
        let id = json!({"controller": controller, "serial": 4});
        assert_eq!(
            events[0].kind,
            EventKind::InputCandidate {
                candidate: id.clone()
            }
        );
        assert_eq!(
            events[1].kind,
            EventKind::InputAdmitted {
                prefix_first_read_us: Some(40),
                candidate: Some(id),
                basis: Some("directed_session_vad_only".into())
            }
        );
        assert!(
            matches!(&events[2].kind, EventKind::InputCandidateRejected { reason: Some(r), .. } if r == "deadline")
        );
        assert_eq!(
            events[3].kind,
            EventKind::RingRequested {
                phase: "listening".into()
            }
        );
        assert_eq!(
            events[3].turn,
            Some(2),
            "ring requests take the owner's turn"
        );
    }

    #[test]
    fn coordinator_vocabulary_maps_without_guessing() {
        let (events, unmapped) = parse_trace(COORDINATOR_RECORDS).unwrap();
        assert_eq!(unmapped, 1, "session_boot is not a scored event");
        let names: Vec<_> = events.iter().map(RuntimeEvent::name).collect();
        assert_eq!(
            names,
            [
                "run_start",
                "listening_ready",
                "input_admitted",
                "local_endpoint",
                "speaker_first_write",
                "turn_cancelled",
                "turn_completed",
                "input_transcript",
                "output_transcript",
                "runtime_fault",
                "run_end"
            ]
        );
        assert_eq!(
            events[5].kind,
            EventKind::TurnCancelled {
                reason: Some("user_interrupted".into()),
                provider_audio_seen: Some(true)
            }
        );
        assert_eq!(
            events[2].kind,
            EventKind::InputAdmitted {
                prefix_first_read_us: Some(5),
                candidate: None,
                basis: None
            }
        );
        assert_eq!(
            events[9].at_us, None,
            "a fault without a timestamp keeps none"
        );
        assert!(
            events
                .iter()
                .all(|e| e.domain == ClockDomain::LampMonotonic)
        );
    }

    #[test]
    fn fake_serialization_round_trips_through_the_trace_parser() {
        let boot = [2_u8; 16];
        let owner = json!({"boot": boot, "turn": 3, "generation": 4});
        for kind in [
            EventKind::InputAdmitted {
                prefix_first_read_us: Some(7),
                candidate: Some(json!({"serial": 1})),
                basis: Some("directed_session_vad_only".into()),
            },
            EventKind::InputCandidate {
                candidate: json!({"serial": 1}),
            },
            EventKind::RingRequested {
                phase: "speaking".into(),
            },
            EventKind::SpeakerFirstWrite,
            EventKind::SpeechRetired,
            EventKind::TurnCancelled {
                reason: Some("user_interrupted".into()),
                provider_audio_seen: Some(true),
            },
            EventKind::TurnCompleted {
                outcome: "audio_written_unscored".into(),
                playback_gaps: 0,
            },
            EventKind::OutputTranscript {
                text: "hi".into(),
                finished: true,
            },
            EventKind::RunEnd {
                status: Some("failed".into()),
                error: Some("x".into()),
            },
        ] {
            let event = RuntimeEvent::new(
                kind,
                Some(3),
                99,
                ClockDomain::LampMonotonic,
                EventSource::Trace,
            );
            let parsed = from_trace(&to_trace(&event, Some(owner.clone()))).unwrap();
            assert_eq!(parsed, event);
        }
    }

    #[test]
    fn cues_require_schema_one_and_consistent_times() {
        let good = br#"{"schema":1,"sequence":2,"kind":"speaker_first_write","boot":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"turn":9,"generation":11,"capture_epoch":3,"reference_epoch_context":8,"event_us":100,"sent_us":101,"expires_us":100100}"#;
        let cue = parse_cue(good).unwrap();
        let event = cue.event().unwrap();
        assert_eq!(event.kind, EventKind::SpeakerFirstWrite);
        assert_eq!((event.turn, event.at_us), (Some(9), Some(100)));
        let stale = br#"{"schema":1,"sequence":2,"kind":"run_end","event_us":100,"sent_us":99,"expires_us":100100}"#;
        assert!(parse_cue(stale).is_err());
        let future = br#"{"schema":2,"sequence":2,"kind":"run_end","event_us":100,"sent_us":100,"expires_us":100100}"#;
        assert!(parse_cue(future).is_err());
    }
    #[test]
    fn directed_cues_preserve_owner_epochs_and_cancel_reason() {
        for (kind, name) in [
            ("input_admitted", "input_admitted"),
            ("local_endpoint", "local_endpoint"),
            ("cancelled", "turn_cancelled"),
        ] {
            let value = json!({"schema":1,"sequence":1,"kind":kind,"boot":vec![7;16],
                "turn":1,"generation":2,"capture_epoch":3,"reference_epoch_context":4,
                "event_us":10,"sent_us":12,"expires_us":100010,
                "reason":if kind == "cancelled" { Some("user_interrupted") } else { None }});
            let cue = parse_cue(&serde_json::to_vec(&value).unwrap()).unwrap();
            assert_eq!(
                (
                    cue.boot,
                    cue.turn,
                    cue.generation,
                    cue.capture_epoch,
                    cue.reference_epoch_context
                ),
                ([7; 16], Some(1), Some(2), Some(3), Some(4))
            );
            assert_eq!(cue.event().unwrap().name(), name);
            if kind == "cancelled" {
                assert!(
                    matches!(cue.event().unwrap().kind, EventKind::TurnCancelled { reason: Some(reason), .. } if reason == "user_interrupted")
                );
            }
            if kind == "cancelled" {
                let mut legacy = value.clone();
                legacy.as_object_mut().unwrap().remove("reason");
                assert!(matches!(
                    parse_cue(&serde_json::to_vec(&legacy).unwrap())
                        .unwrap()
                        .event()
                        .unwrap()
                        .kind,
                    EventKind::TurnCancelled { reason: None, .. }
                ));
            }
            for (field, bad) in [
                ("schema", json!(257)),
                ("sequence", json!(0)),
                ("boot", json!(vec![0; 16])),
                ("turn", Value::Null),
                ("generation", json!(0)),
                ("expires_us", json!(100011)),
                ("sent_us", json!(100010)),
                ("kind", json!("invented")),
            ] {
                let mut invalid = value.clone();
                invalid[field] = bad;
                assert!(
                    parse_cue(&serde_json::to_vec(&invalid).unwrap()).is_err(),
                    "{invalid}"
                );
            }
        }
    }
}
