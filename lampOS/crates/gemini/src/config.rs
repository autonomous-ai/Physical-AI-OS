use crate::{Error, MAX_HANDLE_BYTES, MAX_TEXT_BYTES, Result};
use serde::{Deserialize, Serialize};
use std::{fmt, str::FromStr, sync::Arc, time::Duration};
use tokio_tungstenite::tungstenite::{
    client::IntoClientRequest,
    handshake::client::Request,
    http::{HeaderName, HeaderValue, Uri},
};

pub const GOOGLE_ENDPOINT: &str = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";

#[derive(Clone)]
pub struct Credential {
    name: HeaderName,
    value: HeaderValue,
}
impl Credential {
    pub fn api_key(secret: &str) -> Result<Self> {
        Self::new("x-goog-api-key", "", secret)
    }
    pub fn bearer(secret: &str) -> Result<Self> {
        Self::new("authorization", "Bearer ", secret)
    }
    pub fn ephemeral(secret: &str) -> Result<Self> {
        Self::new("authorization", "Token ", secret)
    }
    fn new(name: &'static str, prefix: &str, secret: &str) -> Result<Self> {
        if secret.is_empty() || secret.len() > 4096 || secret.trim() != secret || !secret.is_ascii()
        {
            return Err(Error::InvalidConfiguration);
        }
        let mut value = HeaderValue::from_str(&format!("{prefix}{secret}"))
            .map_err(|_| Error::InvalidConfiguration)?;
        value.set_sensitive(true);
        Ok(Self {
            name: HeaderName::from_static(name),
            value,
        })
    }
}
impl fmt::Debug for Credential {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Credential([REDACTED])")
    }
}

/// Every wait is finite. These are failure bounds, not latency measurements.
#[derive(Clone, Copy, Debug)]
pub struct Timeouts {
    pub connect: Duration,
    pub setup: Duration,
    pub write: Duration,
    pub read: Duration,
    /// Interruption request to the superseded response's idle completion. Only
    /// the successor's activityEnd waits for it; its audio is never held.
    pub barrier: Duration,
    /// Ended input to the first output of its response.
    pub response: Duration,
    /// Longest silence between outputs before generation completes.
    pub stall: Duration,
    /// Allowance beyond the received audio's real-time duration for the idle
    /// completion. The service defers it while it assumes playback.
    pub completion: Duration,
    /// Ended input to idle completion, whatever progress is observed.
    pub turn: Duration,
    /// How long decoded events may wait for the consumer before the session
    /// fails. The socket is not read meanwhile, so nothing accumulates.
    pub deliver: Duration,
    pub keepalive: Duration,
}
impl Default for Timeouts {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(10),
            setup: Duration::from_secs(10),
            write: Duration::from_millis(200),
            read: Duration::from_secs(60),
            barrier: Duration::from_secs(2),
            response: Duration::from_secs(30),
            stall: Duration::from_secs(10),
            completion: Duration::from_secs(15),
            turn: Duration::from_secs(300),
            deliver: Duration::from_secs(2),
            keepalive: Duration::from_secs(15),
        }
    }
}
impl Timeouts {
    pub(crate) fn validate(self) -> Result<()> {
        for (duration, max) in [
            (self.connect, Duration::from_secs(30)),
            (self.setup, Duration::from_secs(30)),
            (self.write, Duration::from_secs(1)),
            (self.read, Duration::from_secs(300)),
            (self.barrier, Duration::from_secs(5)),
            (self.response, Duration::from_secs(300)),
            (self.stall, Duration::from_secs(60)),
            (self.completion, Duration::from_secs(60)),
            (self.turn, Duration::from_secs(600)),
            (self.deliver, Duration::from_secs(10)),
            (self.keepalive, Duration::from_secs(60)),
        ] {
            if duration < Duration::from_millis(1) || duration > max {
                return Err(Error::InvalidConfiguration);
            }
        }
        if self.keepalive >= self.read {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }
}

/// Explicit provider thinking depth; absence retains the model's own default.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ThinkingLevel {
    Minimal,
    Low,
    Medium,
    High,
}
impl FromStr for ThinkingLevel {
    type Err = Error;
    fn from_str(value: &str) -> Result<Self> {
        match value {
            "MINIMAL" => Ok(Self::Minimal),
            "LOW" => Ok(Self::Low),
            "MEDIUM" => Ok(Self::Medium),
            "HIGH" => Ok(Self::High),
            _ => Err(Error::InvalidConfiguration),
        }
    }
}

/// Whether the opaque server-issued session handle is kept for a reconnect.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum Resumption {
    /// Parse and discard every handle. Setup is unchanged.
    #[default]
    Off,
    /// Keep the latest handle the service volunteers. Initial setup is
    /// unchanged; only a reconnect that presents a handle differs.
    Retain,
    /// Also ask for handles in the initial setup.
    Request,
}

/// What to do when an interrupted request produced no output and the service
/// sends no terminal event for it before `Timeouts::barrier`.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum UnansweredInterruption {
    /// Fail the connection rather than guess which request owns later output.
    #[default]
    Fail,
    /// Treat the silent request as cancelled and let its successor proceed.
    /// Unverified against the live service; see the session reliability notes.
    AssumeCancelled,
}

/// Opaque server token for resuming conversation state on a new connection.
/// Held in memory only: no serialization, no value in Debug output.
#[derive(Clone)]
pub struct ResumptionHandle(Arc<str>);
impl ResumptionHandle {
    pub(crate) fn new(value: &str) -> Option<Self> {
        (!value.is_empty() && value.len() <= MAX_HANDLE_BYTES).then(|| Self(value.into()))
    }
    pub(crate) fn expose(&self) -> &str {
        &self.0
    }
}
impl fmt::Debug for ResumptionHandle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ResumptionHandle([PRIVATE])")
    }
}

#[derive(Clone)]
pub struct SessionConfig {
    pub(crate) endpoint: String,
    pub(crate) credential: Credential,
    pub(crate) model: String,
    pub(crate) voice: String,
    pub(crate) thinking_level: Option<ThinkingLevel>,
    pub(crate) language_code: Option<String>,
    pub(crate) instruction: String,
    pub(crate) timeouts: Timeouts,
    pub(crate) resumption: Resumption,
    pub(crate) resume: Option<ResumptionHandle>,
    pub(crate) unanswered: UnansweredInterruption,
}
impl SessionConfig {
    /// Custom proxies are explicit WSS endpoints. Authentication belongs only in
    /// a private header; userinfo, queries and fragments are rejected entirely.
    pub fn new(endpoint: &str, credential: Credential) -> Result<Self> {
        validate_endpoint(endpoint)?;
        Ok(Self {
            endpoint: endpoint.into(),
            credential,
            model: "gemini-3.8-live".into(),
            voice: "Kore".into(),
            thinking_level: None,
            language_code: None,
            instruction: String::new(),
            timeouts: Timeouts::default(),
            resumption: Resumption::Off,
            resume: None,
            unanswered: UnansweredInterruption::Fail,
        })
    }
    pub fn google(credential: Credential) -> Result<Self> {
        Self::new(GOOGLE_ENDPOINT, credential)
    }
    pub fn model(mut self, model: &str) -> Result<Self> {
        if !identifier(model, 128) {
            return Err(Error::InvalidConfiguration);
        }
        validate_thinking_level(model, self.thinking_level)?;
        self.model = model.into();
        Ok(self)
    }
    pub fn voice(mut self, voice: &str) -> Result<Self> {
        if !identifier(voice, 64) {
            return Err(Error::InvalidConfiguration);
        }
        self.voice = voice.into();
        Ok(self)
    }
    /// Select the model first. Known incompatible explicit requests fail;
    /// unknown proxy model capabilities remain the provider's responsibility.
    pub fn thinking_level(mut self, level: ThinkingLevel) -> Result<Self> {
        validate_thinking_level(&self.model, Some(level))?;
        self.thinking_level = Some(level);
        Ok(self)
    }
    /// Speech language tag, preserved exactly rather than inferred or expanded.
    /// This is not an input-transcription language-hints setting.
    pub fn language_code(mut self, language_code: &str) -> Result<Self> {
        if !valid_language_code(language_code) {
            return Err(Error::InvalidConfiguration);
        }
        self.language_code = Some(language_code.into());
        Ok(self)
    }
    pub fn instruction(mut self, instruction: &str) -> Result<Self> {
        if instruction.len() > MAX_TEXT_BYTES || instruction.contains('\0') {
            return Err(Error::InvalidConfiguration);
        }
        self.instruction = instruction.into();
        Ok(self)
    }
    pub fn timeouts(mut self, timeouts: Timeouts) -> Result<Self> {
        timeouts.validate()?;
        self.timeouts = timeouts;
        Ok(self)
    }
    pub fn resumption(mut self, resumption: Resumption) -> Self {
        self.resumption = resumption;
        self
    }
    /// Present a retained handle in setup. A handle is only ever obtained from
    /// a connection whose configuration allowed retention.
    pub fn resume(mut self, handle: Option<ResumptionHandle>) -> Self {
        self.resume = handle;
        self
    }
    pub fn unanswered_interruption(mut self, policy: UnansweredInterruption) -> Self {
        self.unanswered = policy;
        self
    }
    pub(crate) fn request(&self) -> Result<Request> {
        let mut request = self
            .endpoint
            .as_str()
            .into_client_request()
            .map_err(|_| Error::InvalidConfiguration)?;
        request
            .headers_mut()
            .insert(self.credential.name.clone(), self.credential.value.clone());
        Ok(request)
    }
}
impl fmt::Debug for SessionConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("SessionConfig([PRIVATE])")
    }
}
fn validate_thinking_level(model: &str, level: Option<ThinkingLevel>) -> Result<()> {
    // These exact combinations are evidenced by the pinned V1 setup. Do not
    // infer capabilities of unknown aliases from substrings or clamp silently.
    if level.is_some() && model == "gemini-3.8-live"
        || level == Some(ThinkingLevel::Minimal) && model == "gemini-3.8-live-extended-thinking"
    {
        return Err(Error::InvalidConfiguration);
    }
    Ok(())
}
fn valid_language_code(value: &str) -> bool {
    // Bounded language/script/region/variant form, not a registry allowlist or
    // full BCP-47 extension/private-use parser. The provider checks support.
    if value.len() > 63 {
        return false;
    }
    let mut subtags = value.split('-');
    subtags.next().is_some_and(|language| {
        (2..=8).contains(&language.len()) && language.bytes().all(|b| b.is_ascii_alphabetic())
    }) && subtags.all(|subtag| {
        (2..=8).contains(&subtag.len()) && subtag.bytes().all(|b| b.is_ascii_alphanumeric())
    })
}
fn identifier(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|x| x.is_ascii_alphanumeric() || b"._-".contains(&x))
}
fn validate_endpoint(value: &str) -> Result<()> {
    if value.len() > 2048 || value.contains(['?', '#', '@']) {
        return Err(Error::InvalidConfiguration);
    }
    let uri: Uri = value.parse().map_err(|_| Error::InvalidConfiguration)?;
    if uri.scheme_str() != Some("wss")
        || uri.host().is_none_or(str::is_empty)
        || uri.authority().is_none()
    {
        return Err(Error::InvalidConfiguration);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn authentication_is_sensitive_header_only() {
        let config =
            SessionConfig::google(Credential::api_key("private-only-test-key").unwrap()).unwrap();
        let request = config.request().unwrap();
        assert!(request.headers()["x-goog-api-key"].is_sensitive());
        assert!(!request.uri().to_string().contains("private-only-test-key"));
        assert!(!format!("{request:?}").contains("private-only-test-key"));
        assert_eq!(request.headers()["x-goog-api-key"], "private-only-test-key");
    }
}
