//! Google Live JSON codec. Bounds apply before parsing and before PCM decoding.
//! Field shapes follow https://ai.google.dev/api/live . Unknown optional metadata
//! is ignored; unknown message kinds, tools, malformed audio and auth errors fail
//! with fixed diagnostic categories and no retained raw payload.
use crate::{
    Error, MAX_INPUT_SAMPLES, MAX_OUTPUT_SAMPLES, MAX_TEXT_BYTES, MAX_WIRE_BYTES, Result,
    Resumption, ResumptionHandle, SessionConfig, VoiceActivity,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{
    Deserialize, Deserializer,
    de::{self, IgnoredAny, MapAccess, Visitor},
};
use serde_json::json;
use std::{fmt, time::Duration};

#[derive(Debug)]
pub struct Transcript {
    pub text: String,
    pub finished: bool,
}
#[derive(Debug)]
pub struct Activity {
    pub kind: VoiceActivity,
    pub audio_offset: Option<Duration>,
}
#[derive(Debug)]
pub struct ServerContent {
    pub audio: Vec<i16>,
    pub text: Vec<String>,
    pub input_transcript: Option<Transcript>,
    pub output_transcript: Option<Transcript>,
    pub generation_complete: bool,
    pub interrupted: bool,
    pub turn_complete: bool,
    pub idle: bool,
}
/// Advance notice of a server-side close. `time_left` is absent when the
/// service omits it or sends a form this codec does not read.
#[derive(Debug)]
pub struct GoAway {
    pub time_left: Option<Duration>,
}
/// Only produced by [`decode_server_retaining`]. `handle` is absent when the
/// service reports a point that cannot be resumed.
#[derive(Debug)]
pub struct ResumptionUpdate {
    pub handle: Option<ResumptionHandle>,
    pub resumable: bool,
}
#[derive(Debug)]
pub struct ServerFrame {
    pub setup_complete: bool,
    pub content: Option<ServerContent>,
    pub voice_activity: Option<Activity>,
    pub go_away: Option<GoAway>,
    pub resumption: Option<ResumptionUpdate>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", bound(deserialize = "R: Deserialize<'de>"))]
struct Envelope<R> {
    setup_complete: Option<serde_json::Value>,
    server_content: Option<RawContent>,
    voice_activity: Option<RawActivity>,
    go_away: Option<serde_json::Value>,
    usage_metadata: Option<IgnoredAny>,
    tool_call: Option<IgnoredAny>,
    tool_call_cancellation: Option<IgnoredAny>,
    session_resumption_update: Option<R>,
    error: Option<serde_json::Value>,
}
/// Validates the documented object fields either way. The discarding form
/// never materializes the handle; the retaining form keeps it only inside an
/// opaque in-memory [`ResumptionHandle`].
trait ResumptionMetadata: Sized {
    type Handle: for<'de> Deserialize<'de>;
    fn build(handle: Option<Self::Handle>, resumable: bool) -> Self;
    fn update(self) -> Option<ResumptionUpdate>;
}
struct IgnoredResumptionUpdate;
impl ResumptionMetadata for IgnoredResumptionUpdate {
    type Handle = DiscardedString;
    fn build(_: Option<DiscardedString>, _: bool) -> Self {
        Self
    }
    fn update(self) -> Option<ResumptionUpdate> {
        None
    }
}
struct RetainedResumptionUpdate(ResumptionUpdate);
impl ResumptionMetadata for RetainedResumptionUpdate {
    type Handle = String;
    fn build(handle: Option<String>, resumable: bool) -> Self {
        // A handle reported as not resumable, empty or oversized is not kept.
        Self(ResumptionUpdate {
            handle: handle
                .filter(|_| resumable)
                .as_deref()
                .and_then(ResumptionHandle::new),
            resumable,
        })
    }
    fn update(self) -> Option<ResumptionUpdate> {
        Some(self.0)
    }
}
struct MetadataVisitor<R>(std::marker::PhantomData<R>);
impl<'de, R: ResumptionMetadata> Visitor<'de> for MetadataVisitor<R> {
    type Value = R;
    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a session resumption metadata object")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> std::result::Result<R, A::Error> {
        let mut handle = None;
        let mut handle_seen = false;
        let mut resumable = None;
        while let Some(field) = map.next_key::<ResumptionField>()? {
            match field {
                ResumptionField::NewHandle => {
                    if handle_seen {
                        return Err(de::Error::duplicate_field("newHandle"));
                    }
                    handle_seen = true;
                    handle = Some(map.next_value::<R::Handle>()?);
                }
                ResumptionField::Resumable => {
                    if resumable.is_some() {
                        return Err(de::Error::duplicate_field("resumable"));
                    }
                    resumable = Some(map.next_value::<bool>()?);
                }
                ResumptionField::Other => {
                    let _ = map.next_value::<IgnoredAny>()?;
                }
            }
        }
        Ok(R::build(handle, resumable.unwrap_or(false)))
    }
}
macro_rules! resumption_metadata {
    ($name:ty) => {
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(
                deserializer: D,
            ) -> std::result::Result<Self, D::Error> {
                // deserialize_map rejects array/scalar substitutes for the object.
                deserializer.deserialize_map(MetadataVisitor::<$name>(std::marker::PhantomData))
            }
        }
    };
}
resumption_metadata!(IgnoredResumptionUpdate);
resumption_metadata!(RetainedResumptionUpdate);

#[derive(Deserialize)]
#[serde(field_identifier)]
enum ResumptionField {
    #[serde(rename = "newHandle")]
    NewHandle,
    #[serde(rename = "resumable")]
    Resumable,
    #[serde(other)]
    Other,
}
struct DiscardedString;
impl<'de> Deserialize<'de> for DiscardedString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct StringVisitor;
        impl Visitor<'_> for StringVisitor {
            type Value = DiscardedString;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a string")
            }
            fn visit_str<E: de::Error>(self, _value: &str) -> std::result::Result<Self::Value, E> {
                Ok(DiscardedString)
            }
        }
        deserializer.deserialize_str(StringVisitor)
    }
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct RawContent {
    model_turn: Option<ModelTurn>,
    input_transcription: Option<RawTranscript>,
    output_transcription: Option<RawTranscript>,
    #[serde(default)]
    generation_complete: bool,
    #[serde(default)]
    interrupted: bool,
    #[serde(default)]
    turn_complete: bool,
    interaction_status: Option<String>,
}
#[derive(Deserialize)]
struct ModelTurn {
    parts: Vec<Part>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Part {
    inline_data: Option<Blob>,
    text: Option<String>,
    #[serde(default)]
    thought: bool,
    file_data: Option<IgnoredAny>,
    function_call: Option<IgnoredAny>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Blob {
    mime_type: String,
    data: String,
}
#[derive(Deserialize)]
struct RawTranscript {
    text: String,
    #[serde(default)]
    finished: bool,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawActivity {
    #[serde(rename = "type")]
    kind: String,
    audio_offset: Option<String>,
}

/// Decode one server message, discarding any session resumption handle while
/// parsing. No handle reaches the returned frame.
pub fn decode_server(bytes: &[u8]) -> Result<ServerFrame> {
    decode::<IgnoredResumptionUpdate>(bytes)
}

/// Decode one server message and keep a resumable handle in memory. Used only
/// by a session whose configuration enables [`Resumption`].
pub fn decode_server_retaining(bytes: &[u8]) -> Result<ServerFrame> {
    decode::<RetainedResumptionUpdate>(bytes)
}

/// Fixed category for a server `error` object. The code and status select the
/// category; neither they nor the message text are retained.
fn rejection(error: &serde_json::Value) -> Error {
    let code = error.get("code").and_then(serde_json::Value::as_i64);
    let status = error.get("status").and_then(serde_json::Value::as_str);
    match (code, status) {
        (Some(429), _) | (_, Some("RESOURCE_EXHAUSTED")) => Error::QuotaExceeded,
        (Some(500..=599), _) | (_, Some("UNAVAILABLE" | "INTERNAL")) => Error::ServerUnavailable,
        _ => Error::ServerRejected,
    }
}

fn decode<R: ResumptionMetadata + for<'de> Deserialize<'de>>(bytes: &[u8]) -> Result<ServerFrame> {
    if bytes.len() > MAX_WIRE_BYTES {
        return Err(Error::MessageTooLarge);
    }
    let envelope: Envelope<R> =
        serde_json::from_slice(bytes).map_err(|_| Error::MalformedMessage)?;
    if let Some(error) = &envelope.error {
        return Err(rejection(error));
    }
    if envelope.tool_call.is_some() || envelope.tool_call_cancellation.is_some() {
        return Err(Error::UnsupportedMessage);
    }
    let kinds = usize::from(envelope.setup_complete.is_some())
        + usize::from(envelope.server_content.is_some())
        + usize::from(envelope.go_away.is_some())
        + usize::from(envelope.session_resumption_update.is_some());
    // The service can emit an empty object between content messages. JSON was
    // already validated above; require exactly an empty object, not an unknown
    // message whose fields serde ignored. This no-op grants no readiness or turn.
    let empty_object = bytes
        .iter()
        .filter(|byte| !byte.is_ascii_whitespace())
        .eq(b"{}".iter());
    if kinds > 1
        || (kinds == 0
            && envelope.usage_metadata.is_none()
            && envelope.voice_activity.is_none()
            && !empty_object)
    {
        return Err(Error::MalformedMessage);
    }
    if envelope
        .setup_complete
        .as_ref()
        .is_some_and(|value| !value.is_object())
        || envelope
            .go_away
            .as_ref()
            .is_some_and(|value| !value.is_object())
    {
        return Err(Error::MalformedMessage);
    }
    let content = envelope.server_content.map(decode_content).transpose()?;
    let voice_activity = envelope
        .voice_activity
        .map(|raw| {
            Ok(Activity {
                kind: match raw.kind.as_str() {
                    "ACTIVITY_START" => VoiceActivity::Start,
                    "ACTIVITY_END" => VoiceActivity::End,
                    "TYPE_UNSPECIFIED" => VoiceActivity::Unspecified,
                    _ => return Err(Error::MalformedMessage),
                },
                audio_offset: raw
                    .audio_offset
                    .as_deref()
                    .map(parse_duration)
                    .transpose()?,
            })
        })
        .transpose()?;
    Ok(ServerFrame {
        setup_complete: envelope.setup_complete.is_some(),
        content,
        voice_activity,
        // The notice matters more than its optional duration: an unreadable
        // timeLeft is reported as absent, not as a malformed message.
        go_away: envelope.go_away.map(|raw| GoAway {
            time_left: raw
                .get("timeLeft")
                .and_then(serde_json::Value::as_str)
                .and_then(|value| parse_duration(value).ok()),
        }),
        resumption: envelope
            .session_resumption_update
            .and_then(ResumptionMetadata::update),
    })
}

fn decode_content(raw: RawContent) -> Result<ServerContent> {
    let idle = match raw.interaction_status.as_deref() {
        None | Some("INTERACTION_STATUS_UNSPECIFIED") | Some("IDLE") | Some("REQUIRES_ACTION") => {
            true
        }
        Some("IN_PROGRESS") => false,
        Some(_) => return Err(Error::MalformedMessage),
    };
    if raw.interaction_status.is_some() && !raw.turn_complete {
        return Err(Error::MalformedMessage);
    }
    let mut audio = Vec::new();
    let mut text = Vec::new();
    let mut text_bytes = 0;
    if let Some(turn) = raw.model_turn {
        if turn.parts.len() > 8 {
            return Err(Error::MessageTooLarge);
        }
        for part in turn.parts {
            if part.function_call.is_some() || part.file_data.is_some() {
                return Err(Error::UnsupportedMessage);
            }
            if part.inline_data.is_some() && part.text.is_some() {
                return Err(Error::MalformedMessage);
            }
            if part.thought {
                continue;
            }
            if let Some(blob) = part.inline_data {
                if !output_mime(&blob.mime_type) {
                    return Err(Error::InvalidAudio);
                }
                let remaining_bytes = (MAX_OUTPUT_SAMPLES - audio.len()) * 2;
                if blob.data.len() > remaining_bytes.div_ceil(3) * 4 {
                    return Err(Error::MessageTooLarge);
                }
                let pcm = STANDARD
                    .decode(blob.data)
                    .map_err(|_| Error::InvalidAudio)?;
                if pcm.is_empty() || pcm.len() % 2 != 0 || pcm.len() > remaining_bytes {
                    return Err(Error::InvalidAudio);
                }
                audio.extend(
                    pcm.chunks_exact(2)
                        .map(|pair| i16::from_le_bytes([pair[0], pair[1]])),
                );
            }
            if let Some(value) = part.text {
                text_bytes += value.len();
                if text_bytes > MAX_TEXT_BYTES {
                    return Err(Error::MessageTooLarge);
                }
                text.push(value);
            }
        }
    }
    Ok(ServerContent {
        audio,
        text,
        input_transcript: raw.input_transcription.map(transcript).transpose()?,
        output_transcript: raw.output_transcription.map(transcript).transpose()?,
        generation_complete: raw.generation_complete,
        interrupted: raw.interrupted,
        turn_complete: raw.turn_complete,
        idle,
    })
}
fn transcript(raw: RawTranscript) -> Result<Transcript> {
    if raw.text.len() > MAX_TEXT_BYTES {
        return Err(Error::MessageTooLarge);
    }
    Ok(Transcript {
        text: raw.text,
        finished: raw.finished,
    })
}
fn output_mime(mime: &str) -> bool {
    let mut parts = mime.split(';').map(str::trim);
    if parts.next() != Some("audio/pcm") {
        return false;
    }
    let mut rate = false;
    let mut channels = false;
    for part in parts {
        match part {
            "rate=24000" if !rate => rate = true,
            "channels=1" if !channels => channels = true,
            _ => return false,
        }
    }
    rate
}
fn parse_duration(raw: &str) -> Result<Duration> {
    let value = raw.strip_suffix('s').ok_or(Error::MalformedMessage)?;
    let (seconds, fraction) = value.split_once('.').unwrap_or((value, ""));
    if seconds.is_empty()
        || !seconds.bytes().all(|x| x.is_ascii_digit())
        || fraction.len() > 9
        || !fraction.bytes().all(|x| x.is_ascii_digit())
    {
        return Err(Error::MalformedMessage);
    }
    let seconds: u64 = seconds.parse().map_err(|_| Error::MalformedMessage)?;
    if seconds > 86_400 {
        return Err(Error::MalformedMessage);
    }
    let nanos = if fraction.is_empty() {
        0
    } else {
        fraction
            .parse::<u32>()
            .map_err(|_| Error::MalformedMessage)?
            * 10_u32.pow(9 - fraction.len() as u32)
    };
    Ok(Duration::new(seconds, nanos))
}

pub fn encode_setup(config: &SessionConfig) -> Result<String> {
    // No tools, search or provider-side admission policy. Session resumption
    // appears only when explicitly requested or when presenting a handle.
    let mut setup = json!({"setup": {
        "model": format!("models/{}", config.model),
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": config.voice}}}
        },
        "systemInstruction": {"parts": [{"text": config.instruction}]},
        "realtimeInputConfig": {
            "automaticActivityDetection": {"disabled": true},
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY"
        },
        "inputAudioTranscription": {}, "outputAudioTranscription": {}
    }});
    if let Some(level) = config.thinking_level {
        setup["setup"]["generationConfig"]["thinkingConfig"] = json!({
            "thinkingLevel": level,
            "includeThoughts": false
        });
    }
    if let Some(language) = &config.language_code {
        setup["setup"]["generationConfig"]["speechConfig"]["languageCode"] = json!(language);
    }
    if let Some(handle) = &config.resume {
        setup["setup"]["sessionResumption"] = json!({"handle": handle.expose()});
    } else if config.resumption == Resumption::Request {
        setup["setup"]["sessionResumption"] = json!({});
    }
    serde_json::to_string(&setup).map_err(|_| Error::InvalidConfiguration)
}
pub fn encode_audio(samples: &[i16]) -> Result<String> {
    if samples.is_empty() || samples.len() > MAX_INPUT_SAMPLES {
        return Err(Error::InvalidAudio);
    }
    let bytes: Vec<u8> = samples
        .iter()
        .flat_map(|sample| sample.to_le_bytes())
        .collect();
    serde_json::to_string(&json!({"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": STANDARD.encode(bytes)}}})).map_err(|_| Error::InvalidAudio)
}
pub const ACTIVITY_START: &str = r#"{"realtimeInput":{"activityStart":{}}}"#;
pub const ACTIVITY_END: &str = r#"{"realtimeInput":{"activityEnd":{}}}"#;
