//! Reviewed room-audio annotations. Acoustic latency exists only where a person
//! listened to the continuous room recording and marked both boundaries; the
//! runner's software timestamps can locate a region but never supply them.
use crate::{
    Result,
    evaluate::Metric,
    import::{VerifiedRoom, verify_room_evidence},
    invalid,
    record::{AttemptRecord, StepStatus},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const SCHEMA: u32 = 1;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Annotations {
    pub schema: u32,
    pub run_id: String,
    pub annotator: String,
    /// How boundaries were found, for example "listening plus spectrogram".
    pub method: String,
    pub attempts: BTreeMap<String, AttemptAnnotation>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttemptAnnotation {
    /// SHA-256 of the room WAV the times refer to.
    pub room_recording_sha256: Option<String>,
    /// True only after a person listened to the attempt.
    pub listened: bool,
    pub steps: BTreeMap<String, StepAnnotation>,
    #[serde(default)]
    pub notes: Option<String>,
}

/// Times are seconds in the room WAV timebase.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StepAnnotation {
    /// Software hint for locating the stimulus; never used for scoring.
    #[serde(default)]
    pub hint_stimulus_start_s: Option<f64>,
    pub user_speech_end_s: Option<f64>,
    pub user_speech_end_uncertainty_ms: Option<f64>,
    /// First substantive answer word; fillers, acknowledgments and cues do not count.
    pub first_substantive_answer_word_s: Option<f64>,
    pub answer_onset_uncertainty_ms: Option<f64>,
    pub interruption_onset_s: Option<f64>,
    pub lamp_silent_s: Option<f64>,
    pub silence_uncertainty_ms: Option<f64>,
    pub answer_relevant: Option<bool>,
    pub answer_complete: Option<bool>,
    pub spoken_failure_notice: Option<bool>,
    #[serde(default)]
    pub notes: Option<String>,
}

impl StepAnnotation {
    fn values_valid(&self, duration_s: f64) -> bool {
        let boundaries = [
            self.user_speech_end_s,
            self.first_substantive_answer_word_s,
            self.interruption_onset_s,
            self.lamp_silent_s,
        ];
        let uncertainties = [
            self.user_speech_end_uncertainty_ms,
            self.answer_onset_uncertainty_ms,
            self.silence_uncertainty_ms,
        ];
        boundaries
            .into_iter()
            .all(|v| v.is_none_or(|v| v.is_finite() && (0.0..=duration_s).contains(&v)))
            && uncertainties.into_iter().all(|v| {
                v.is_none_or(|v| v.is_finite() && (0.0..=duration_s * 1000.0).contains(&v))
            })
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AcousticScore {
    pub step: String,
    pub metric: Option<Metric>,
    pub value_ms: Option<f64>,
    pub uncertainty_ms: Option<f64>,
    pub unscored_reason: Option<String>,
    pub answer_relevant: Option<bool>,
}

impl AcousticScore {
    pub fn unscored(step: &str, reason: &str) -> Self {
        Self {
            step: step.into(),
            metric: None,
            value_ms: None,
            uncertainty_ms: None,
            unscored_reason: Some(reason.into()),
            answer_relevant: None,
        }
    }
    pub fn measurement(&self) -> Option<(Metric, f64)> {
        Some((self.metric?, self.value_ms?))
    }
}

/// Room-audio reference retained by a physical backend.
pub fn room_audio(record: &AttemptRecord) -> Option<(&str, bool)> {
    let room = record.evidence.get("room_audio")?;
    Some((room.get("sha256")?.as_str()?, room.get("valid")?.as_bool()?))
}

impl Annotations {
    pub fn parse(bytes: &[u8]) -> Result<Self> {
        if bytes.len() > 16_000_000 {
            return Err(invalid("annotation file exceeds 16 MB"));
        }
        let annotations: Self = serde_json::from_slice(bytes)?;
        if annotations.schema != SCHEMA {
            return Err(invalid("unsupported annotation schema"));
        }
        for (attempt, entry) in &annotations.attempts {
            for (step, s) in &entry.steps {
                if !s.values_valid(f64::MAX / 1000.0) {
                    return Err(invalid(&format!(
                        "invalid annotation time in {attempt}/{step}"
                    )));
                }
            }
        }
        Ok(annotations)
    }

    fn verified_attempt<'a>(
        &'a self,
        record: &AttemptRecord,
    ) -> Result<(&'a AttemptAnnotation, VerifiedRoom)> {
        if !record.stratum.is_physical() {
            return Err(invalid("no room audio: fake runtime"));
        }
        let entry = self
            .attempts
            .get(&record.attempt_id)
            .ok_or_else(|| invalid("no annotation for this attempt"))?;
        if self.schema != SCHEMA || self.run_id != record.run_id {
            return Err(invalid(
                "annotation schema or run_id does not match this attempt",
            ));
        }
        if !entry.listened {
            return Err(invalid("attempt not listened to"));
        }
        let room = record
            .evidence
            .get("room_audio")
            .filter(|room| !room.is_null())
            .ok_or_else(|| invalid("no continuous room recording"))?;
        let verified = verify_room_evidence(room)?;
        if entry.room_recording_sha256.as_deref() != Some(verified.sha256.as_str()) {
            return Err(invalid("annotation refers to a different room recording"));
        }
        Ok((entry, verified))
    }

    /// The step's annotation, only when it may be used: physical attempt,
    /// reverified room recording, matching run/hash, listened, and finite
    /// in-recording values. Reversed intervals invalidate timing, not an
    /// independent content judgment from this same valid recording.
    pub fn reviewed_step(&self, record: &AttemptRecord, step: &str) -> Option<&StepAnnotation> {
        let (entry, verified) = self.verified_attempt(record).ok()?;
        entry
            .steps
            .get(step)
            .filter(|s| s.values_valid(verified.duration_s))
    }

    pub fn score(&self, record: &AttemptRecord) -> Vec<AcousticScore> {
        let steps = record
            .steps
            .iter()
            .filter(|s| s.status == StepStatus::Injected && s.scene.is_some());
        let unscored = |reason: &str| {
            steps
                .clone()
                .map(|s| AcousticScore::unscored(&s.step, reason))
                .collect()
        };
        let (entry, verified) = match self.verified_attempt(record) {
            Ok(value) => value,
            Err(error) => return unscored(&error.to_string()),
        };
        let mut scores = Vec::new();
        for step in steps {
            let Some(s) = entry.steps.get(&step.step) else {
                scores.push(AcousticScore::unscored(&step.step, "step not annotated"));
                continue;
            };
            if !s.values_valid(verified.duration_s) {
                scores.push(AcousticScore::unscored(
                    &step.step,
                    "annotation has nonfinite or out-of-recording values",
                ));
                continue;
            }
            let mut scored = false;
            if s.user_speech_end_s
                .zip(s.first_substantive_answer_word_s)
                .is_some_and(|(end, word)| word < end)
                || s.interruption_onset_s
                    .zip(s.lamp_silent_s)
                    .is_some_and(|(onset, silent)| silent < onset)
            {
                scores.push(AcousticScore::unscored(
                    &step.step,
                    "negative interval; check the annotation",
                ));
                continue;
            }
            if let (Some(end), Some(word)) =
                (s.user_speech_end_s, s.first_substantive_answer_word_s)
            {
                scores.push(AcousticScore {
                    step: step.step.clone(),
                    metric: Some(Metric::AcousticSpeechEndToAnswer),
                    value_ms: Some((word - end) * 1000.0),
                    uncertainty_ms: Some(
                        s.user_speech_end_uncertainty_ms.unwrap_or(0.0)
                            + s.answer_onset_uncertainty_ms.unwrap_or(0.0),
                    ),
                    unscored_reason: None,
                    answer_relevant: s.answer_relevant,
                });
                scored = true;
            }
            if let (Some(onset), Some(silent)) = (s.interruption_onset_s, s.lamp_silent_s) {
                scores.push(AcousticScore {
                    step: step.step.clone(),
                    metric: Some(Metric::AcousticInterruptionToSilence),
                    value_ms: Some((silent - onset) * 1000.0),
                    uncertainty_ms: s.silence_uncertainty_ms,
                    unscored_reason: None,
                    answer_relevant: s.answer_relevant,
                });
                scored = true;
            }
            if !scored {
                let mut score = AcousticScore::unscored(&step.step, "a boundary is missing");
                score.answer_relevant = s.answer_relevant;
                scores.push(score);
            }
        }
        scores
    }
}

/// A fill-in template for every physical attempt with room audio.
pub fn template(run_id: &str, records: &[AttemptRecord]) -> Annotations {
    let mut attempts = BTreeMap::new();
    for record in records.iter().filter(|r| r.stratum.is_physical()) {
        let Some((sha256, _)) = room_audio(record) else {
            continue;
        };
        let start = record
            .evidence
            .get("room_audio")
            .and_then(|room| room.get("capture_requested_runner_us"))
            .and_then(Value::as_u64);
        let steps = record
            .steps
            .iter()
            .filter(|s| s.status == StepStatus::Injected)
            .map(|s| {
                let hint = start
                    .zip(s.started_us)
                    .map(|(start, at)| (at as f64 - start as f64) / 1e6);
                (
                    s.step.clone(),
                    StepAnnotation {
                        hint_stimulus_start_s: hint,
                        ..StepAnnotation::default()
                    },
                )
            })
            .collect();
        attempts.insert(
            record.attempt_id.clone(),
            AttemptAnnotation {
                room_recording_sha256: Some(sha256.into()),
                listened: false,
                steps,
                notes: None,
            },
        );
    }
    Annotations {
        schema: SCHEMA,
        run_id: run_id.into(),
        annotator: String::new(),
        method: String::new(),
        attempts,
    }
}
