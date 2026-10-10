use crate::{Error, MAX_TEXT_BYTES, Result};
use serde::{Deserialize, Serialize};
use std::{fmt, str::FromStr, time::Duration};
use tokio_tungstenite::tungstenite::{
    client::IntoClientRequest,
    handshake::client::Request,
    http::{HeaderName, HeaderValue, Uri},
};

pub const GOOGLE_ENDPOINT: &str = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";

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

#[derive(Clone, Copy, Debug)]
pub struct Timeouts {
    pub connect: Duration,
    pub setup: Duration,
    pub write: Duration,
    pub read: Duration,
    pub barrier: Duration,
    pub response: Duration,
    pub keepalive: Duration,
}
impl Default for Timeouts {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(10),
            setup: Duration::from_secs(10),
            write: Duration::from_millis(200),
            read: Duration::from_secs(60),
            barrier: Duration::from_millis(800),
            response: Duration::from_secs(90),
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
            (self.barrier, Duration::from_secs(1)),
            (self.response, Duration::from_secs(300)),
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

pub struct SessionConfig {
    pub(crate) endpoint: String,
    pub(crate) credential: Credential,
    pub(crate) model: String,
    pub(crate) voice: String,
    pub(crate) thinking_level: Option<ThinkingLevel>,
    pub(crate) language_code: Option<String>,
    pub(crate) instruction: String,
    pub(crate) timeouts: Timeouts,
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
