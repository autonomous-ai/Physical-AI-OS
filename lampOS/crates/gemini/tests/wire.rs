use base64::{Engine, engine::general_purpose::STANDARD};
use lamp_gemini::{
    Credential, Error, GOOGLE_ENDPOINT, MAX_INPUT_SAMPLES, MAX_OUTPUT_SAMPLES, MAX_TEXT_BYTES,
    MAX_WIRE_BYTES, SessionConfig, Timeouts, wire,
};
use serde_json::{Value, json};
use std::time::Duration;

#[test]
fn configuration_rejects_insecure_or_credential_bearing_endpoints_and_redacts_debug() {
    for endpoint in [
        "ws://localhost/live",
        "https://localhost/live",
        "wss://user:password@host/live",
        "wss://host/live?key=secret",
        "wss://host/live#secret",
        "wss:///live",
    ] {
        assert!(
            SessionConfig::new(endpoint, Credential::api_key("private-test-key").unwrap()).is_err()
        );
    }
    let configuration = SessionConfig::new(
        "wss://proxy.example/live",
        Credential::bearer("private-test-key").unwrap(),
    )
    .unwrap();
    assert!(!format!("{configuration:?}").contains("private-test-key"));
    assert!(
        SessionConfig::google(Credential::api_key("key").unwrap())
            .unwrap()
            .model("model/../escape")
            .is_err()
    );
    assert!(
        SessionConfig::google(Credential::api_key("key").unwrap())
            .unwrap()
            .voice("")
            .is_err()
    );
    assert!(
        SessionConfig::google(Credential::api_key("key").unwrap())
            .unwrap()
            .instruction(&"a".repeat(MAX_TEXT_BYTES + 1))
            .is_err()
    );
    for value in ["", " leading", "bad\r\nHeader: injected"] {
        assert!(Credential::api_key(value).is_err());
    }
    assert!(
        SessionConfig::google(Credential::api_key("key").unwrap())
            .unwrap()
            .timeouts(Timeouts {
                connect: Duration::ZERO,
                ..Timeouts::default()
            })
            .is_err()
    );
}

#[test]
fn setup_is_manual_audio_only_and_contains_no_authentication_or_tools() {
    let configuration = SessionConfig::new(
        GOOGLE_ENDPOINT,
        Credential::api_key("NEVER-IN-JSON").unwrap(),
    )
    .unwrap()
    .instruction("Answer briefly.")
    .unwrap();
    let bytes = wire::encode_setup(&configuration).unwrap();
    assert!(!bytes.contains("NEVER-IN-JSON"));
    let value: Value = serde_json::from_str(&bytes).unwrap();
    assert_eq!(value["setup"]["model"], "models/gemini-3.8-live");
    assert_eq!(
        value["setup"]["generationConfig"]["responseModalities"],
        json!(["AUDIO"])
    );
    assert_eq!(
        value["setup"]["realtimeInputConfig"]["automaticActivityDetection"]["disabled"],
        true
    );
    assert!(value["setup"].get("tools").is_none());
    assert!(value["setup"].get("proactivity").is_none());
    assert!(value["setup"].get("sessionResumption").is_none());
}

#[test]
fn signed_pcm_is_little_endian_and_size_bounded_in_both_directions() {
    let encoded = wire::encode_audio(&[i16::MIN, 0, i16::MAX]).unwrap();
    let value: Value = serde_json::from_str(&encoded).unwrap();
    assert_eq!(
        STANDARD
            .decode(value["realtimeInput"]["audio"]["data"].as_str().unwrap())
            .unwrap(),
        [0, 128, 0, 0, 255, 127]
    );
    assert!(wire::encode_audio(&[]).is_err());
    assert!(wire::encode_audio(&vec![0; MAX_INPUT_SAMPLES + 1]).is_err());
    let frame = json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000;channels=1","data":STANDARD.encode([0,128,255,127])}}]}}});
    assert_eq!(
        wire::decode_server(frame.to_string().as_bytes())
            .unwrap()
            .content
            .unwrap()
            .audio,
        [i16::MIN, i16::MAX]
    );
    let too_large = json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000","data":STANDARD.encode(vec![0; (MAX_OUTPUT_SAMPLES + 1)*2])}}]}}});
    assert!(wire::decode_server(too_large.to_string().as_bytes()).is_err());
}

#[test]
fn malformed_pcm_unknown_messages_and_conflicting_envelopes_fail_closed() {
    for (mime, data) in [
        ("audio/pcm;rate=16000", "AAA="),
        ("audio/pcm", "AAA="),
        ("audio/pcm;rate=24000", "AA=="),
        ("audio/pcm;rate=24000", "not base64!"),
    ] {
        let frame = json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":mime,"data":data}}]}}});
        assert!(wire::decode_server(frame.to_string().as_bytes()).is_err());
    }
    for frame in [
        json!({"unknownMessage":{}}),
        json!({"setupComplete":false}),
        json!({"setupComplete":{},"serverContent":{}}),
        json!({"toolCall":{"functionCalls":[]}}),
        json!({"voiceActivity":{"type":"UNRECOGNIZED"}}),
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"UNRECOGNIZED"}}),
    ] {
        assert!(wire::decode_server(frame.to_string().as_bytes()).is_err());
    }
    assert_eq!(
        wire::decode_server(&vec![b' '; MAX_WIRE_BYTES + 1]).unwrap_err(),
        Error::MessageTooLarge
    );
    assert!(wire::decode_server(br#"{"setupComplete":{},"setupComplete":{}}"#).is_err());
    assert!(wire::decode_server(&[0xff, 0xfe]).is_err());
}

#[test]
fn text_parts_and_audio_parts_have_aggregate_bounds() {
    let part = json!({"inlineData":{"mimeType":"audio/pcm;rate=24000","data":STANDARD.encode(vec![0; MAX_OUTPUT_SAMPLES])}});
    let frame = json!({"serverContent":{"modelTurn":{"parts":[part.clone(),part.clone(),part]}}});
    assert!(wire::decode_server(frame.to_string().as_bytes()).is_err());
    let frame = json!({"serverContent":{"modelTurn":{"parts":vec![json!({"text":"a"});9]}}});
    assert!(wire::decode_server(frame.to_string().as_bytes()).is_err());
    let frame =
        json!({"serverContent":{"outputTranscription":{"text":"x".repeat(MAX_TEXT_BYTES+1)}}});
    assert!(wire::decode_server(frame.to_string().as_bytes()).is_err());
    let frame = json!({"serverContent":{"modelTurn":{"parts":[{"text":"private thought","thought":true}]}}});
    assert!(
        wire::decode_server(frame.to_string().as_bytes())
            .unwrap()
            .content
            .unwrap()
            .text
            .is_empty()
    );
}

#[test]
fn resumption_updates_are_typed_bounded_metadata_and_never_retain_handles() {
    for update in [
        json!({}),
        json!({"newHandle":"", "resumable":false}),
        json!({"newHandle":"PRIVATE-HANDLE\nWITH-ESCAPES", "resumable":true}),
    ] {
        let bytes = json!({"sessionResumptionUpdate": update}).to_string();
        let frame = wire::decode_server(bytes.as_bytes()).unwrap();
        assert!(!frame.setup_complete);
        assert!(frame.content.is_none());
        assert!(frame.voice_activity.is_none());
        assert!(!frame.go_away);
        assert!(!format!("{frame:?}").contains("PRIVATE-HANDLE"));
    }
    let oversized = json!({"sessionResumptionUpdate":{"newHandle":"x".repeat(MAX_WIRE_BYTES),"resumable":true}});
    assert_eq!(
        wire::decode_server(oversized.to_string().as_bytes()).unwrap_err(),
        Error::MessageTooLarge
    );
}

#[test]
fn malformed_resumption_metadata_and_tool_siblings_remain_rejected() {
    for update in [
        json!(false),
        json!("not an object"),
        json!([]),
        json!(["handle", true]),
        json!({"newHandle":7}),
        json!({"newHandle":{}}),
        json!({"resumable":"true"}),
    ] {
        let bytes = json!({"sessionResumptionUpdate":update}).to_string();
        assert_eq!(
            wire::decode_server(bytes.as_bytes()).unwrap_err(),
            Error::MalformedMessage
        );
    }
    for sibling in ["setupComplete", "serverContent", "goAway"] {
        let mut frame = json!({"sessionResumptionUpdate":{"newHandle":"PRIVATE","resumable":true}});
        frame[sibling] = json!({});
        assert_eq!(
            wire::decode_server(frame.to_string().as_bytes()).unwrap_err(),
            Error::MalformedMessage
        );
    }
    for sibling in ["toolCall", "toolCallCancellation"] {
        let mut frame = json!({"sessionResumptionUpdate":{"newHandle":"PRIVATE","resumable":true}});
        frame[sibling] = json!({});
        assert_eq!(
            wire::decode_server(frame.to_string().as_bytes()).unwrap_err(),
            Error::UnsupportedMessage
        );
    }
    for bytes in [
        br#"{"sessionResumptionUpdate":{"newHandle":"a","newHandle":"b"}}"#.as_slice(),
        br#"{"sessionResumptionUpdate":{"resumable":true,"resumable":false}}"#.as_slice(),
        br#"{"sessionResumptionUpdate":{},"sessionResumptionUpdate":{}}"#.as_slice(),
    ] {
        assert_eq!(
            wire::decode_server(bytes).unwrap_err(),
            Error::MalformedMessage
        );
    }
}

#[test]
fn only_empty_json_objects_are_accepted_as_empty_server_messages() {
    for bytes in [b"{}".as_slice(), b" { } ", b"\r\n{\t\n}\r\n"] {
        let frame = wire::decode_server(bytes).unwrap();
        assert!(!frame.setup_complete);
        assert!(frame.content.is_none());
        assert!(frame.voice_activity.is_none());
        assert!(!frame.go_away);
    }
    let content = wire::decode_server(br#"{"serverContent":{}}"#)
        .unwrap()
        .content
        .unwrap();
    assert!(content.audio.is_empty());
    assert!(content.text.is_empty());
    assert!(content.input_transcript.is_none());
    assert!(content.output_transcript.is_none());
    assert!(!content.generation_complete);
    assert!(!content.turn_complete);
    assert!(!content.interrupted);
    for bytes in [
        b"".as_slice(),
        b" ",
        b"null",
        b"[]",
        br#""{}""#,
        br#"{"unknownMessage":{}}"#,
        br#"{"unknownMessage":null}"#,
        br#"{"serverContent":null}"#,
        br#"{"setupComplete":null}"#,
        b"{\x0b}",
        b"{}{}",
    ] {
        assert_eq!(
            wire::decode_server(bytes).unwrap_err(),
            Error::MalformedMessage
        );
    }
}
