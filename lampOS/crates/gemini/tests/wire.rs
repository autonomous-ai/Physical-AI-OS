use base64::{Engine, engine::general_purpose::STANDARD};
use lamp_gemini::{
    Credential, Error, GOOGLE_ENDPOINT, MAX_HANDLE_BYTES, MAX_INPUT_SAMPLES, MAX_OUTPUT_SAMPLES,
    MAX_TEXT_BYTES, MAX_WIRE_BYTES, Resumption, SessionConfig, Timeouts, wire,
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
    // Every wait has a ceiling; none can be configured away.
    for timeouts in [
        Timeouts {
            barrier: Duration::from_secs(6),
            ..Timeouts::default()
        },
        Timeouts {
            stall: Duration::ZERO,
            ..Timeouts::default()
        },
        Timeouts {
            completion: Duration::from_secs(61),
            ..Timeouts::default()
        },
        Timeouts {
            turn: Duration::from_secs(601),
            ..Timeouts::default()
        },
        Timeouts {
            deliver: Duration::from_secs(11),
            ..Timeouts::default()
        },
    ] {
        assert!(
            SessionConfig::google(Credential::api_key("key").unwrap())
                .unwrap()
                .timeouts(timeouts)
                .is_err()
        );
    }
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
        assert!(frame.go_away.is_none());
        assert!(frame.resumption.is_none());
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
        assert!(frame.go_away.is_none());
        assert!(frame.resumption.is_none());
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

#[test]
fn retaining_decode_keeps_only_a_resumable_bounded_handle_and_never_prints_it() {
    let frame = wire::decode_server_retaining(
        br#"{"sessionResumptionUpdate":{"newHandle":"PRIVATE-HANDLE","resumable":true}}"#,
    )
    .unwrap();
    let update = frame.resumption.as_ref().unwrap();
    assert!(update.resumable && update.handle.is_some());
    assert!(!format!("{frame:?}").contains("PRIVATE-HANDLE"));
    for (update, resumable) in [
        (json!({}), false),
        (json!({"newHandle":"", "resumable":true}), true),
        (
            json!({"newHandle":"PRIVATE-HANDLE", "resumable":false}),
            false,
        ),
        (json!({"newHandle":"PRIVATE-HANDLE"}), false),
        (
            json!({"newHandle":"x".repeat(MAX_HANDLE_BYTES + 1), "resumable":true}),
            true,
        ),
    ] {
        let bytes = json!({"sessionResumptionUpdate": update}).to_string();
        let update = wire::decode_server_retaining(bytes.as_bytes())
            .unwrap()
            .resumption
            .unwrap();
        assert!(update.handle.is_none());
        assert_eq!(update.resumable, resumable);
    }
    // The same shape validation applies whether or not the handle is kept.
    for bytes in [
        br#"{"sessionResumptionUpdate":{"newHandle":7}}"#.as_slice(),
        br#"{"sessionResumptionUpdate":["handle",true]}"#.as_slice(),
        br#"{"sessionResumptionUpdate":{"newHandle":"a","newHandle":"b"}}"#.as_slice(),
        br#"{"sessionResumptionUpdate":{"resumable":"true"}}"#.as_slice(),
    ] {
        assert_eq!(
            wire::decode_server_retaining(bytes).unwrap_err(),
            Error::MalformedMessage
        );
    }
}

#[test]
fn resumption_enters_setup_only_when_requested_or_when_presenting_a_handle() {
    let configuration = || {
        SessionConfig::new(
            GOOGLE_ENDPOINT,
            Credential::api_key("NEVER-IN-JSON").unwrap(),
        )
    };
    let setup = |configuration: &SessionConfig| -> Value {
        serde_json::from_str(&wire::encode_setup(configuration).unwrap()).unwrap()
    };
    let default = setup(&configuration().unwrap());
    let retain = setup(&configuration().unwrap().resumption(Resumption::Retain));
    assert_eq!(default, retain);
    assert!(default["setup"].get("sessionResumption").is_none());
    let request = setup(&configuration().unwrap().resumption(Resumption::Request));
    assert_eq!(request["setup"]["sessionResumption"], json!({}));
    let handle = wire::decode_server_retaining(
        br#"{"sessionResumptionUpdate":{"newHandle":"HANDLE-ONE","resumable":true}}"#,
    )
    .unwrap()
    .resumption
    .unwrap()
    .handle;
    let resumed = setup(&configuration().unwrap().resume(handle));
    assert_eq!(
        resumed["setup"]["sessionResumption"],
        json!({"handle":"HANDLE-ONE"})
    );
    // Nothing else in the setup depends on resumption.
    let mut without = resumed.clone();
    without["setup"]
        .as_object_mut()
        .unwrap()
        .remove("sessionResumption");
    assert_eq!(without, default);
}

#[test]
fn go_away_notice_is_typed_and_its_duration_is_optional() {
    for (notice, expected) in [
        (json!({}), None),
        (json!({"timeLeft":"30s"}), Some(Duration::from_secs(30))),
        (
            json!({"timeLeft":"1.500s"}),
            Some(Duration::from_millis(1500)),
        ),
        (json!({"timeLeft":"soon"}), None),
        (json!({"timeLeft":{"seconds":30}}), None),
    ] {
        let bytes = json!({"goAway": notice}).to_string();
        let frame = wire::decode_server(bytes.as_bytes()).unwrap();
        assert_eq!(frame.go_away.unwrap().time_left, expected);
        assert!(frame.content.is_none());
    }
    for notice in [json!(true), json!("30s"), json!([]), json!(null)] {
        let bytes = json!({"goAway": notice}).to_string();
        assert_eq!(
            wire::decode_server(bytes.as_bytes()).unwrap_err(),
            Error::MalformedMessage
        );
    }
}

#[test]
fn server_errors_map_to_fixed_categories_from_code_and_status_only() {
    for (error, expected) in [
        (
            json!({"code":429,"message":"PRIVATE"}),
            Error::QuotaExceeded,
        ),
        (
            json!({"status":"RESOURCE_EXHAUSTED","message":"PRIVATE"}),
            Error::QuotaExceeded,
        ),
        (json!({"code":503}), Error::ServerUnavailable),
        (json!({"status":"INTERNAL"}), Error::ServerUnavailable),
        (
            json!({"code":403,"message":"PRIVATE"}),
            Error::ServerRejected,
        ),
        (
            json!({"code":400,"status":"INVALID_ARGUMENT"}),
            Error::ServerRejected,
        ),
        (json!("PRIVATE"), Error::ServerRejected),
        (json!({}), Error::ServerRejected),
    ] {
        let bytes = json!({"error": error}).to_string();
        let decoded = wire::decode_server(bytes.as_bytes()).unwrap_err();
        assert_eq!(decoded, expected);
        assert!(!format!("{decoded:?} {decoded}").contains("PRIVATE"));
    }
}
