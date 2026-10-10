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
    InputAdmitted {
        prefix_first_read_us: Option<u64>,
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
    /// Cues do not carry the reason; the final trace does.
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
            EventKind::InputAdmitted { .. } => "input_admitted",
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
        "input_admitted" => EventKind::InputAdmitted {
            prefix_first_read_us: u64_field(record, "prefix_first_host_read_us"),
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

/// lamp-live cue datagram (`fixture_provider::CueSink`, schema 1).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Cue {
    pub schema: u8,
    pub sequence: u64,
    pub kind: String,
    pub turn: Option<u64>,
    pub generation: Option<u64>,
    pub event_us: u64,
    pub sent_us: u64,
    pub expires_us: u64,
}

pub fn parse_cue(bytes: &[u8]) -> Result<Cue> {
    if bytes.len() > 512 {
        return Err(invalid("cue exceeds 512 bytes"));
    }
    let value: Value = serde_json::from_slice(bytes)?;
    let cue = Cue {
        schema: value["schema"].as_u64().unwrap_or(0) as u8,
        sequence: value["sequence"]
            .as_u64()
            .ok_or_else(|| invalid("cue without sequence"))?,
        kind: value["kind"]
            .as_str()
            .ok_or_else(|| invalid("cue without kind"))?
            .to_owned(),
        turn: value["turn"].as_u64(),
        generation: value["generation"].as_u64(),
        event_us: value["event_us"]
            .as_u64()
            .ok_or_else(|| invalid("cue without event time"))?,
        sent_us: value["sent_us"]
            .as_u64()
            .ok_or_else(|| invalid("cue without send time"))?,
        expires_us: value["expires_us"]
            .as_u64()
            .ok_or_else(|| invalid("cue without expiry"))?,
    };
    if cue.schema != 1 || cue.sent_us < cue.event_us || cue.expires_us <= cue.event_us {
        return Err(invalid("unsupported or inconsistent cue"));
    }
    Ok(cue)
}

impl Cue {
    pub fn event(&self) -> Option<RuntimeEvent> {
        let kind = match self.kind.as_str() {
            "listening_ready" => EventKind::ListeningReady,
            "speaker_first_write" => EventKind::SpeakerFirstWrite,
            "speech_retired" => EventKind::SpeechRetired,
            "cancelled" => EventKind::TurnCancelled {
                reason: None,
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
        EventKind::InputAdmitted {
            prefix_first_read_us,
        } => {
            json!({"kind":"input_admitted","owner":owner,"prefix_first_host_read_us":prefix_first_read_us})
        }
        EventKind::ProviderInputStarted => {
            json!({"kind":"provider_input_started","waiting_for_barrier":false})
        }
        EventKind::LocalEndpoint { last_block_read_us } => json!({"kind":"local_endpoint",
            "last_block_host_read_us":last_block_read_us,"includes_silence_wait_ms":600,"acoustic_speech_end":null}),
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
                prefix_first_read_us: Some(5)
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
}
