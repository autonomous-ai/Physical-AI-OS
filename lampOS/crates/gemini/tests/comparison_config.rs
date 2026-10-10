use lamp_gemini::{Credential, Error, SessionConfig, ThinkingLevel, wire};
use serde_json::{Value, json};

fn configuration() -> SessionConfig {
    SessionConfig::new(
        "wss://proxy.example/live",
        Credential::api_key("TEST-ONLY-NOT-A-SECRET").unwrap(),
    )
    .unwrap()
}
fn setup(configuration: &SessionConfig) -> Value {
    serde_json::from_str(&wire::encode_setup(configuration).unwrap()).unwrap()
}

#[test]
fn omitted_options_preserve_the_complete_existing_setup() {
    let config = configuration().instruction("Answer briefly.").unwrap();
    let expected = json!({"setup": {
        "model": "models/gemini-3.8-live",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}
        },
        "systemInstruction": {"parts": [{"text": "Answer briefly."}]},
        "realtimeInputConfig": {
            "automaticActivityDetection": {"disabled": true},
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY"
        },
        "inputAudioTranscription": {}, "outputAudioTranscription": {}
    }});
    assert_eq!(wire::encode_setup(&config).unwrap(), expected.to_string());
    let extended = setup(
        &configuration()
            .model("gemini-3.8-live-extended-thinking")
            .unwrap(),
    );
    assert!(
        extended["setup"]["generationConfig"]
            .get("thinkingConfig")
            .is_none()
    );
    assert!(
        extended["setup"]["generationConfig"]["speechConfig"]
            .get("languageCode")
            .is_none()
    );
}

#[test]
fn matched_v1_setup_places_explicit_fields_in_the_google_generation_config() {
    let config = configuration()
        .model("gemini-3.8-live-extended-thinking")
        .unwrap()
        .voice("Kore")
        .unwrap()
        .thinking_level(ThinkingLevel::Low)
        .unwrap()
        .language_code("en-US")
        .unwrap();
    let value = setup(&config);
    assert_eq!(
        value["setup"]["model"],
        "models/gemini-3.8-live-extended-thinking"
    );
    assert_eq!(
        value["setup"]["generationConfig"],
        json!({
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}},
                "languageCode": "en-US"
            },
            "thinkingConfig": {"thinkingLevel": "LOW", "includeThoughts": false}
        })
    );
    assert!(value["setup"].get("thinkingConfig").is_none());
    assert_eq!(value["setup"]["inputAudioTranscription"], json!({}));
    assert_eq!(value["setup"]["outputAudioTranscription"], json!({}));
    assert!(value["setup"].get("tools").is_none());
    assert!(
        !wire::encode_setup(&config)
            .unwrap()
            .contains("TEST-ONLY-NOT-A-SECRET")
    );
}

#[test]
fn thinking_levels_parse_and_serialize_only_explicit_protocol_values() {
    for (text, level) in [
        ("MINIMAL", ThinkingLevel::Minimal),
        ("LOW", ThinkingLevel::Low),
        ("MEDIUM", ThinkingLevel::Medium),
        ("HIGH", ThinkingLevel::High),
    ] {
        assert_eq!(text.parse::<ThinkingLevel>().unwrap(), level);
        assert_eq!(
            serde_json::from_value::<ThinkingLevel>(json!(text)).unwrap(),
            level
        );
        assert_eq!(serde_json::to_value(level).unwrap(), json!(text));
        let value = setup(
            &configuration()
                .model("proxy-model-alias")
                .unwrap()
                .thinking_level(level)
                .unwrap(),
        );
        assert_eq!(
            value["setup"]["generationConfig"]["thinkingConfig"],
            json!({
                "thinkingLevel": text, "includeThoughts": false
            })
        );
    }
    for text in [
        "",
        "low",
        " Low",
        "LOW ",
        "0",
        "AUTO",
        "THINKING_LEVEL_UNSPECIFIED",
        "LOW\n",
    ] {
        assert_eq!(
            text.parse::<ThinkingLevel>().unwrap_err(),
            Error::InvalidConfiguration
        );
        assert!(serde_json::from_value::<ThinkingLevel>(json!(text)).is_err());
    }
    for value in [json!(null), json!(1), json!({}), json!(["LOW"])] {
        assert!(serde_json::from_value::<ThinkingLevel>(value).is_err());
    }
}

#[test]
fn known_incompatible_requests_fail_without_omission_clamping_or_model_change_bypass() {
    for level in [
        ThinkingLevel::Minimal,
        ThinkingLevel::Low,
        ThinkingLevel::Medium,
        ThinkingLevel::High,
    ] {
        assert!(configuration().thinking_level(level).is_err());
        assert!(
            configuration()
                .model("proxy-model-alias")
                .unwrap()
                .thinking_level(level)
                .unwrap()
                .model("gemini-3.8-live")
                .is_err()
        );
    }
    assert!(
        configuration()
            .model("gemini-3.8-live-extended-thinking")
            .unwrap()
            .thinking_level(ThinkingLevel::Minimal)
            .is_err()
    );
    assert!(
        configuration()
            .model("proxy-model-alias")
            .unwrap()
            .thinking_level(ThinkingLevel::Minimal)
            .unwrap()
            .model("gemini-3.8-live-extended-thinking")
            .is_err()
    );
    for level in [
        ThinkingLevel::Low,
        ThinkingLevel::Medium,
        ThinkingLevel::High,
    ] {
        assert!(
            configuration()
                .model("gemini-3.8-live-extended-thinking")
                .unwrap()
                .thinking_level(level)
                .is_ok()
        );
    }
}

#[test]
fn unknown_proxy_aliases_do_not_inherit_substring_capability_rules() {
    for model in [
        "my-gemini-3.8-live-alias",
        "proxy-native-audio-test",
        "proxy-extended-thinking-test",
        "proxy-2.5-test",
    ] {
        let value = setup(
            &configuration()
                .model(model)
                .unwrap()
                .thinking_level(ThinkingLevel::Minimal)
                .unwrap()
                .language_code("en")
                .unwrap(),
        );
        assert_eq!(value["setup"]["model"], format!("models/{model}"));
        assert_eq!(
            value["setup"]["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "MINIMAL"
        );
        assert_eq!(
            value["setup"]["generationConfig"]["speechConfig"]["languageCode"],
            "en"
        );
    }
    assert_eq!(
        wire::decode_server(br#"{"error":{"message":"unsupported private model field"}}"#)
            .unwrap_err(),
        Error::ServerRejected
    );
}

#[test]
fn language_tags_are_bounded_preserved_and_independent_of_thinking_and_stt_hints() {
    for language in [
        "en",
        "vi",
        "en-US",
        "vi-VN",
        "cmn-CN",
        "zh-Hant-TW",
        "es-419",
        "EN-us",
    ] {
        let value = setup(&configuration().language_code(language).unwrap());
        assert_eq!(
            value["setup"]["generationConfig"]["speechConfig"]["languageCode"],
            language
        );
        assert!(
            value["setup"]["generationConfig"]
                .get("thinkingConfig")
                .is_none()
        );
        assert_eq!(value["setup"]["inputAudioTranscription"], json!({}));
    }
    for language in [
        "",
        "en_US",
        " en-US",
        "en-US ",
        "en\nUS",
        "en\0",
        "-en",
        "en-",
        "en--US",
        "12-US",
        "éñ-US",
        "e-US",
        "abcdefghi-US",
        "en-abcdefghi",
        "en-u-ca-gregory",
    ] {
        assert!(
            configuration().language_code(language).is_err(),
            "accepted {language:?}"
        );
    }
    let limit = format!("eng{}-abcde", "-abcdefgh".repeat(6));
    assert_eq!(limit.len(), 63);
    assert!(configuration().language_code(&limit).is_ok());
    assert!(configuration().language_code(&(limit + "f")).is_err());
}
