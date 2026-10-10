use lamp_gemini::{Error, wire};
use lamp_live::config::{CHAT_INSTRUCTION, ProviderConfig};
use serde_json::{Value, json};
use std::{
    fs::{self, DirBuilder, OpenOptions},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt, symlink},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

const MARKER: &str = "TEST-ONLY-PROVIDER-CONFIG-CREDENTIAL";
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Private {
    root: PathBuf,
    config: PathBuf,
    credential: PathBuf,
}
impl Private {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "lamp-provider-config-{}-{}-{}",
            std::process::id(),
            lamp_ipc::monotonic_ns(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&root).unwrap();
        let fixture = Self {
            config: root.join("provider.json"),
            credential: root.join("credential"),
            root,
        };
        fixture.create(&fixture.credential, format!("{MARKER}\n").as_bytes());
        fixture.create(&fixture.config, b"{}");
        fixture
    }
    fn create(&self, path: &std::path::Path, bytes: &[u8]) {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .unwrap()
            .write_all(bytes)
            .unwrap();
    }
    fn value(&self) -> Value {
        json!({
            "endpoint": "wss://proxy.example/live",
            "model": "gemini-3.8-live",
            "voice": "Kore",
            "credential_file": self.credential,
        })
    }
    fn write(&self, value: &Value) {
        fs::write(&self.config, serde_json::to_vec(value).unwrap()).unwrap();
    }
    fn setup(&self, value: &Value) -> Value {
        self.write(value);
        let session = ProviderConfig::load(&self.config)
            .unwrap()
            .into_session()
            .unwrap();
        assert!(!format!("{session:?}").contains(MARKER));
        let encoded = wire::encode_setup(&session).unwrap();
        assert!(!encoded.contains(MARKER));
        assert!(!encoded.contains(self.credential.to_str().unwrap()));
        serde_json::from_str(&encoded).unwrap()
    }
    fn invalid_session(&self, value: &Value) {
        self.write(value);
        let error = ProviderConfig::load(&self.config)
            .unwrap()
            .into_session()
            .unwrap_err();
        assert_eq!(
            error.downcast_ref::<Error>(),
            Some(&Error::InvalidConfiguration)
        );
        assert!(!error.to_string().contains(MARKER));
    }
}
impl Drop for Private {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[test]
fn private_file_to_setup_preserves_explicit_model_thinking_and_language() {
    let fixture = Private::new();
    let mut value = fixture.value();
    value["model"] = json!("gemini-3.8-live-extended-thinking");
    value["thinking_level"] = json!("LOW");
    value["language_code"] = json!("en-US");
    let setup = fixture.setup(&value);
    assert_eq!(
        setup["setup"]["model"],
        "models/gemini-3.8-live-extended-thinking"
    );
    assert_eq!(
        setup["setup"]["generationConfig"],
        json!({
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}},
                "languageCode": "en-US"
            },
            "thinkingConfig": {"thinkingLevel": "LOW", "includeThoughts": false}
        })
    );
    assert_eq!(
        setup["setup"]["systemInstruction"],
        json!({"parts": [{"text": CHAT_INSTRUCTION}]})
    );
    assert!(setup["setup"].get("thinkingConfig").is_none());
    assert_eq!(setup["setup"]["inputAudioTranscription"], json!({}));
    assert_eq!(setup["setup"]["outputAudioTranscription"], json!({}));
}

#[test]
fn omitted_or_null_options_preserve_existing_setup_and_authentication_choices() {
    let fixture = Private::new();
    let default = fixture.setup(&fixture.value());
    assert_eq!(default["setup"]["model"], "models/gemini-3.8-live");
    assert_eq!(
        default["setup"]["generationConfig"],
        json!({
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}
        })
    );
    for authentication in ["api_key", "bearer", "ephemeral"] {
        let mut value = fixture.value();
        value["authentication"] = json!(authentication);
        assert_eq!(fixture.setup(&value), default);
        value["thinking_level"] = Value::Null;
        value["language_code"] = Value::Null;
        assert_eq!(fixture.setup(&value), default);
    }
}

#[test]
fn typed_load_rejects_malformed_levels_language_types_and_unknown_fields() {
    let fixture = Private::new();
    for (field, value) in [
        ("thinking_level", json!("low")),
        ("thinking_level", json!("LOW ")),
        ("thinking_level", json!("THINKING_LEVEL_UNSPECIFIED")),
        ("thinking_level", json!(0)),
        ("thinking_level", json!({})),
        ("language_code", json!(["en-US"])),
        ("language_code", json!(true)),
        ("languageCode", json!("en-US")),
        ("thinkingLevel", json!("LOW")),
        ("authentication", json!("unknown")),
    ] {
        let mut configuration = fixture.value();
        configuration[field] = value;
        fixture.write(&configuration);
        let error = ProviderConfig::load(&fixture.config).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData, "{field}");
        assert!(!error.to_string().contains(MARKER));
    }
}

#[test]
fn session_conversion_rejects_known_unsupported_combinations_and_invalid_tags() {
    let fixture = Private::new();
    let mut plain = fixture.value();
    plain["thinking_level"] = json!("LOW");
    fixture.invalid_session(&plain);
    let mut minimal = fixture.value();
    minimal["model"] = json!("gemini-3.8-live-extended-thinking");
    minimal["thinking_level"] = json!("MINIMAL");
    fixture.invalid_session(&minimal);
    for language in ["", "en_US", " en-US", "en--US", "en\nUS", "en-u-ca-gregory"] {
        let mut value = fixture.value();
        value["language_code"] = json!(language);
        fixture.invalid_session(&value);
    }
    let mut unknown = fixture.value();
    unknown["model"] = json!("proxy-extended-thinking-alias");
    unknown["thinking_level"] = json!("MINIMAL");
    unknown["language_code"] = json!("vi");
    let setup = fixture.setup(&unknown);
    assert_eq!(
        setup["setup"]["generationConfig"]["thinkingConfig"]["thinkingLevel"],
        "MINIMAL"
    );
    assert_eq!(
        setup["setup"]["generationConfig"]["speechConfig"]["languageCode"],
        "vi"
    );
}

#[test]
fn configuration_must_be_private_bounded_regular_and_not_a_symlink() {
    let fixture = Private::new();
    fixture.write(&fixture.value());
    fs::set_permissions(&fixture.config, fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(
        ProviderConfig::load(&fixture.config).err().unwrap().kind(),
        io::ErrorKind::PermissionDenied
    );
    fs::set_permissions(&fixture.config, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&fixture.config, vec![b' '; 16_385]).unwrap();
    assert_eq!(
        ProviderConfig::load(&fixture.config).err().unwrap().kind(),
        io::ErrorKind::PermissionDenied
    );
    assert!(ProviderConfig::load(&fixture.root).is_err());
    fixture.write(&fixture.value());
    let link = fixture.root.join("config-link");
    symlink(&fixture.config, &link).unwrap();
    assert!(ProviderConfig::load(&link).is_err());
    let mut relative = fixture.value();
    relative["credential_file"] = json!("credential");
    fixture.write(&relative);
    assert_eq!(
        ProviderConfig::load(&fixture.config).err().unwrap().kind(),
        io::ErrorKind::InvalidInput
    );
}

#[test]
fn credential_file_guards_and_redacted_failures_are_preserved() {
    let fixture = Private::new();
    fixture.write(&fixture.value());
    fs::set_permissions(&fixture.credential, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(
        ProviderConfig::load(&fixture.config)
            .unwrap()
            .into_session()
            .is_err()
    );
    fs::set_permissions(&fixture.credential, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&fixture.credential, vec![b'x'; 4099]).unwrap();
    assert!(
        ProviderConfig::load(&fixture.config)
            .unwrap()
            .into_session()
            .is_err()
    );
    fs::write(&fixture.credential, MARKER).unwrap();
    let link = fixture.root.join("credential-link");
    symlink(&fixture.credential, &link).unwrap();
    let mut value = fixture.value();
    value["credential_file"] = json!(link);
    fixture.write(&value);
    assert!(
        ProviderConfig::load(&fixture.config)
            .unwrap()
            .into_session()
            .is_err()
    );
    value["credential_file"] = json!(fixture.root);
    fixture.write(&value);
    assert!(
        ProviderConfig::load(&fixture.config)
            .unwrap()
            .into_session()
            .is_err()
    );
    for invalid in [format!(" {MARKER}"), format!("{MARKER}\nInjected: value")] {
        fs::write(&fixture.credential, invalid).unwrap();
        fixture.invalid_session(&fixture.value());
    }
}
