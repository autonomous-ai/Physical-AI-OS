use super::*;
use crate::{Credential, GOOGLE_ENDPOINT};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use tokio::io::DuplexStream;
use tokio_tungstenite::tungstenite::protocol::{
    Role,
    frame::{
        Frame,
        coding::{Data, OpCode},
    },
};

type MockSocket = WebSocketStream<DuplexStream>;
fn configuration(timeouts: Timeouts) -> SessionConfig {
    SessionConfig::new(
        GOOGLE_ENDPOINT,
        Credential::api_key("test-only-secret").unwrap(),
    )
    .unwrap()
    .timeouts(timeouts)
    .unwrap()
}
async fn pair(capacity: usize) -> (MockSocket, MockSocket) {
    let (client, server) = tokio::io::duplex(capacity);
    (
        WebSocketStream::from_raw_socket(client, Role::Client, Some(socket_config())).await,
        WebSocketStream::from_raw_socket(server, Role::Server, Some(socket_config())).await,
    )
}
async fn mock(timeouts: Timeouts) -> (Connection, MockSocket) {
    let (client, mut server) = pair(MAX_WIRE_BYTES * 2).await;
    let connect = tokio::spawn(setup_and_spawn(
        client,
        configuration(timeouts),
        SessionId::new(17).unwrap(),
    ));
    let setup = receive(&mut server).await;
    assert_eq!(
        setup["setup"]["realtimeInputConfig"]["automaticActivityDetection"]["disabled"],
        true
    );
    assert!(!setup.to_string().contains("test-only-secret"));
    server
        .send(Message::Binary(br#"{"setupComplete":{}}"#.to_vec().into()))
        .await
        .unwrap();
    (connect.await.unwrap().unwrap(), server)
}
async fn receive(server: &mut MockSocket) -> Value {
    let message = timeout(Duration::from_secs(2), server.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    serde_json::from_slice(&message.into_data()).unwrap()
}
async fn send_json(server: &mut MockSocket, value: Value) {
    server.send(Message::text(value.to_string())).await.unwrap();
}
async fn event(connection: &mut Connection) -> Event {
    timeout(Duration::from_secs(2), connection.next_event())
        .await
        .unwrap()
        .unwrap()
}
async fn ended_turn(connection: &mut Connection, server: &mut MockSocket, id: u64) -> RequestId {
    let request = RequestId::new(id).unwrap();
    let input = connection.input();
    input.try_start(request).unwrap();
    assert!(
        receive(server).await["realtimeInput"]
            .get("activityStart")
            .is_some()
    );
    assert!(
        matches!(event(connection).await, Event::InputStarted { lineage, .. } if lineage.request == request)
    );
    input
        .try_audio(request, 10, Instant::now(), &[1; 160])
        .unwrap();
    assert_eq!(
        receive(server).await["realtimeInput"]["audio"]["mimeType"],
        "audio/pcm;rate=16000"
    );
    input.try_end(request).unwrap();
    assert!(
        receive(server).await["realtimeInput"]
            .get("activityEnd")
            .is_some()
    );
    request
}
fn audio(value: i16) -> Value {
    json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000","data":STANDARD.encode(value.to_le_bytes())}}]}}})
}
async fn disconnected(connection: &Connection) -> Error {
    let mut state = connection.subscribe_state();
    timeout(Duration::from_secs(2), async {
        loop {
            if let State::Disconnected(error) = *state.borrow_and_update() {
                return error;
            }
            state.changed().await.unwrap();
        }
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn readiness_requires_setup_complete_and_setup_wait_is_bounded() {
    let (client, mut server) = pair(65_536).await;
    let timeouts = Timeouts {
        setup: Duration::from_millis(40),
        ..Timeouts::default()
    };
    let connect = tokio::spawn(setup_and_spawn(
        client,
        configuration(timeouts),
        SessionId::new(1).unwrap(),
    ));
    receive(&mut server).await;
    send_json(&mut server, json!({})).await;
    send_json(&mut server, json!({"usageMetadata":{"totalTokenCount":1}})).await;
    send_json(
        &mut server,
        json!({"sessionResumptionUpdate":{"newHandle":"test-only-handle","resumable":true}}),
    )
    .await;
    assert!(!connect.is_finished());
    assert!(matches!(connect.await.unwrap(), Err(Error::SetupTimeout)));
}

#[tokio::test]
async fn output_before_setup_is_never_readiness() {
    let (client, mut server) = pair(65_536).await;
    let connect = tokio::spawn(setup_and_spawn(
        client,
        configuration(Timeouts::default()),
        SessionId::new(1).unwrap(),
    ));
    receive(&mut server).await;
    send_json(&mut server, audio(4)).await;
    assert!(matches!(
        connect.await.unwrap(),
        Err(Error::UnexpectedResponse)
    ));
}

#[tokio::test]
async fn audio_keeps_session_and_request_lineage_and_generation_is_not_retirement() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 4).await;
    send_json(&mut server, audio(-32768)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, sequence: 0, pcm } if lineage == Lineage { session: SessionId::new(17).unwrap(), request } && pcm == [-32768])
    );
    send_json(
        &mut server,
        json!({"serverContent":{"generationComplete":true}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::GenerationComplete { lineage } if lineage.request == request)
    );
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IN_PROGRESS"}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle:false } if lineage.request == request)
    );
    send_json(&mut server, audio(9)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, sequence:1, pcm } if lineage.request == request && pcm == [9])
    );
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle:true } if lineage.request == request)
    );
    send_json(&mut server, audio(8)).await;
    assert_eq!(disconnected(&connection).await, Error::UnexpectedResponse);
}

#[tokio::test]
async fn interruption_holds_new_pcm_and_discards_old_audio_until_idle_barrier() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    let new = RequestId::new(2).unwrap();
    let input = connection.input();
    input.try_start(new).unwrap();
    input
        .try_audio(
            new,
            0,
            Instant::now() - Duration::from_millis(200),
            &[2; 160],
        )
        .unwrap();
    input.try_end(new).unwrap();
    assert!(
        receive(&mut server).await["realtimeInput"]
            .get("activityStart")
            .is_some()
    );
    assert!(
        matches!(event(&mut connection).await, Event::InputStarted { lineage, waiting_for_barrier:true } if lineage.request == new)
    );
    assert!(
        timeout(Duration::from_millis(20), server.next())
            .await
            .is_err()
    );
    send_json(&mut server, audio(111)).await;
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    assert!(
        matches!(event(&mut connection).await, Event::Interrupted { lineage } if lineage.request == old)
    );
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IN_PROGRESS"}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle:false } if lineage.request == old)
    );
    assert!(
        timeout(Duration::from_millis(20), server.next())
            .await
            .is_err()
    );
    send_json(&mut server, audio(112)).await;
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle:true } if lineage.request == old)
    );
    assert!(
        receive(&mut server).await["realtimeInput"]
            .get("audio")
            .is_some()
    );
    assert!(
        receive(&mut server).await["realtimeInput"]
            .get("activityEnd")
            .is_some()
    );
    send_json(&mut server, audio(222)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == new && pcm == [222])
    );
    assert_eq!(
        input.try_audio(old, 11, Instant::now(), &[0; 160]),
        Err(Error::StaleRequest)
    );
}

#[tokio::test]
async fn missing_old_barrier_fails_instead_of_assigning_new_audio() {
    let timeouts = Timeouts {
        barrier: Duration::from_millis(50),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    let new = RequestId::new(2).unwrap();
    connection.input().try_start(new).unwrap();
    connection
        .input()
        .try_audio(new, 0, Instant::now(), &[0; 160])
        .unwrap();
    connection.input().try_end(new).unwrap();
    receive(&mut server).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::InputStarted { .. }
    ));
    send_json(&mut server, audio(200)).await;
    assert_eq!(disconnected(&connection).await, Error::BarrierTimeout);
    assert!(connection.next_event().await.is_none());
}

#[tokio::test]
async fn retirement_filters_already_queued_output_and_does_not_wait_for_network() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    // Lifecycle event proves the previous audio was decoded and queued first.
    send_json(
        &mut server,
        json!({"serverContent":{"generationComplete":true}}),
    )
    .await;
    timeout(Duration::from_secs(2), async {
        while connection.events.len() < 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    connection.input().retire(old);
    assert!(
        matches!(event(&mut connection).await, Event::GenerationComplete { lineage } if lineage.request == old)
    );
    assert_eq!(connection.input().try_start(old), Err(Error::StaleRequest));
}

#[tokio::test]
async fn output_backpressure_disconnects_without_unbounded_buffering() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    for _ in 0..EVENT_QUEUE_CAPACITY + 1 {
        send_json(&mut server, audio(1)).await;
    }
    assert_eq!(disconnected(&connection).await, Error::Backpressure);
    assert_eq!(connection.events.len(), EVENT_QUEUE_CAPACITY);
    assert!(connection.next_event().await.is_none());
}

#[tokio::test]
async fn input_backpressure_and_stale_or_future_audio_are_explicit() {
    let (connection, _server) = mock(Timeouts::default()).await;
    let input = connection.input();
    let request = RequestId::new(1).unwrap();
    input.try_start(request).unwrap();
    assert_eq!(
        input.try_audio(request, 0, Instant::now() - MAX_INPUT_AGE, &[0]),
        Err(Error::StaleInput)
    );
    assert_eq!(
        input.try_audio(request, 0, Instant::now() + Duration::from_secs(1), &[0]),
        Err(Error::StaleInput)
    );
    assert_eq!(
        input.try_audio(request, 0, Instant::now(), &[]),
        Err(Error::InvalidAudio)
    );
    // No await lets the bounded producer fill before the actor can drain it.
    for sequence in 0..INPUT_QUEUE_CAPACITY - 1 {
        input
            .try_audio(request, sequence as u64, Instant::now(), &[0])
            .unwrap();
    }
    assert_eq!(
        input.try_audio(request, 100, Instant::now(), &[0]),
        Err(Error::Backpressure)
    );
    assert_eq!(connection.state(), State::Disconnected(Error::Backpressure));
}

#[tokio::test]
async fn discontinuity_and_overlapping_inputs_close_the_session() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = RequestId::new(1).unwrap();
    connection.input().try_start(request).unwrap();
    receive(&mut server).await;
    event(&mut connection).await;
    connection
        .input()
        .try_audio(request, 4, Instant::now(), &[0])
        .unwrap();
    receive(&mut server).await;
    connection
        .input()
        .try_audio(request, 6, Instant::now(), &[0])
        .unwrap();
    assert_eq!(disconnected(&connection).await, Error::InputSequence);
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    connection.input().try_start(request).unwrap();
    receive(&mut server).await;
    event(&mut connection).await;
    connection
        .input()
        .try_start(RequestId::new(2).unwrap())
        .unwrap();
    assert_eq!(disconnected(&connection).await, Error::OverlappingInput);
}

#[tokio::test]
async fn binary_fragmented_json_is_decoded_with_the_same_bounds() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    let json = audio(32).to_string().into_bytes();
    let split = json.len() / 2;
    server
        .send(Message::Frame(Frame::message(
            json[..split].to_vec(),
            OpCode::Data(Data::Binary),
            false,
        )))
        .await
        .unwrap();
    server
        .send(Message::Frame(Frame::message(
            json[split..].to_vec(),
            OpCode::Data(Data::Continue),
            true,
        )))
        .await
        .unwrap();
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == request && pcm == [32])
    );
}

#[tokio::test]
async fn incoming_transcripts_and_vad_remain_uncorrelated_observations() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    send_json(&mut server, json!({"serverContent":{"inputTranscription":{"text":"ambient words","finished":true}},"voiceActivity":{"type":"ACTIVITY_START","audioOffset":"1.250s"}})).await;
    assert!(
        matches!(event(&mut connection).await, Event::VoiceActivity { session, kind:crate::VoiceActivity::Start, audio_offset:Some(value) } if session.get() == 17 && value == Duration::from_millis(1250))
    );
    assert!(
        matches!(event(&mut connection).await, Event::UncorrelatedInputTranscript { session, text, finished:true } if session.get() == 17 && text == "ambient words")
    );
}

#[tokio::test]
async fn read_response_and_write_waits_have_finite_deadlines() {
    let timeouts = Timeouts {
        read: Duration::from_millis(50),
        keepalive: Duration::from_millis(10),
        ..Timeouts::default()
    };
    let (connection, _server) = mock(timeouts).await;
    assert_eq!(disconnected(&connection).await, Error::ReadTimeout);
    let timeouts = Timeouts {
        response: Duration::from_millis(40),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    assert_eq!(disconnected(&connection).await, Error::ResponseTimeout);
    let (mut client, _server) = pair(16).await;
    assert_eq!(
        send(
            &mut client,
            Message::text("x".repeat(4096)),
            Duration::from_millis(20)
        )
        .await,
        Err(Error::WriteTimeout)
    );
}

#[tokio::test]
async fn close_reasons_and_server_errors_are_not_exposed() {
    let (connection, mut server) = mock(Timeouts::default()).await;
    send_json(
        &mut server,
        json!({"error":{"code":403,"message":"SECRET MUST NOT ESCAPE"}}),
    )
    .await;
    let error = disconnected(&connection).await;
    assert_eq!(error, Error::ServerRejected);
    assert!(!format!("{error:?} {error}").contains("SECRET"));
    let (connection, mut server) = mock(Timeouts::default()).await;
    server
        .close(Some(tungstenite::protocol::CloseFrame {
            code: tungstenite::protocol::frame::coding::CloseCode::Policy,
            reason: "PRIVATE CLOSE REASON".into(),
        }))
        .await
        .unwrap();
    assert_eq!(
        disconnected(&connection).await,
        Error::PeerClosed { code: Some(1008) }
    );
}

#[tokio::test]
async fn pending_input_has_a_sample_bound_and_preserves_acquisition_age() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    let request = RequestId::new(2).unwrap();
    let input = connection.input();
    input.try_start(request).unwrap();
    receive(&mut server).await;
    event(&mut connection).await;
    for sequence in 0..=MAX_PENDING_SAMPLES / 160 {
        if input
            .try_audio(request, sequence as u64, Instant::now(), &[0; 160])
            .is_err()
        {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(disconnected(&connection).await, Error::Backpressure);
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    connection.input().try_start(request).unwrap();
    connection
        .input()
        .try_audio(
            request,
            0,
            Instant::now() - Duration::from_millis(900),
            &[0; 160],
        )
        .unwrap();
    receive(&mut server).await;
    event(&mut connection).await;
    assert_eq!(disconnected(&connection).await, Error::StaleInput);
}

#[tokio::test]
async fn connection_deadline_covers_stalled_tls_without_any_cloud_call() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (_stream, _) = listener.accept().await.unwrap();
        tokio::time::sleep(Duration::from_secs(1)).await;
    });
    let config = SessionConfig::new(
        &format!("wss://{address}/live"),
        Credential::api_key("local-test-key").unwrap(),
    )
    .unwrap()
    .timeouts(Timeouts {
        connect: Duration::from_millis(30),
        ..Timeouts::default()
    })
    .unwrap();
    assert!(matches!(
        connect(config, SessionId::new(1).unwrap()).await,
        Err(Error::ConnectTimeout)
    ));
    server.abort();
}

#[test]
fn dependency_payload_logging_is_disabled_in_debug_and_release_profiles() {
    assert!(log::STATIC_MAX_LEVEL <= log::LevelFilter::Info);
}

#[tokio::test]
async fn immediate_post_setup_close_keeps_code_when_event_eof_is_observed_first() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    assert_eq!(connection.state(), State::Ready);
    server
        .close(Some(tungstenite::protocol::CloseFrame {
            code: 4029.into(),
            reason: "PRIVATE BACKEND REASON".into(),
        }))
        .await
        .unwrap();
    // Match the worker: wait for events, not the state-change notification.
    assert!(
        timeout(Duration::from_secs(2), connection.next_event())
            .await
            .unwrap()
            .is_none()
    );
    let state = connection.state();
    assert_eq!(
        state,
        State::Disconnected(Error::PeerClosed { code: Some(4029) })
    );
    assert!(!format!("{state:?}").contains("PRIVATE"));
}

#[tokio::test]
async fn immediate_post_setup_rejection_keeps_backend_category_at_event_eof() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    send_json(
        &mut server,
        json!({"error":{"code":403,"message":"PRIVATE AUTH DETAIL"}}),
    )
    .await;
    assert!(
        timeout(Duration::from_secs(2), connection.next_event())
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        connection.state(),
        State::Disconnected(Error::ServerRejected)
    );
    assert!(!format!("{:?}", connection.state()).contains("PRIVATE"));
}

#[tokio::test]
async fn connected_idle_session_retains_event_channel_without_input() {
    let (mut connection, _server) = mock(Timeouts::default()).await;
    assert!(
        timeout(Duration::from_millis(40), connection.next_event())
            .await
            .is_err()
    );
    assert_eq!(connection.state(), State::Ready);
    connection.shutdown();
    assert!(
        timeout(Duration::from_secs(2), connection.next_event())
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(connection.state(), State::Closed);
}

#[tokio::test]
async fn post_setup_resumption_metadata_preserves_the_first_input_and_reply() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    send_json(&mut server, json!({"sessionResumptionUpdate":{"newHandle":"test-only-private-handle","resumable":true}})).await;
    // A metadata update emits no event or client resumption message. The next
    // client message remains this admitted activityStart, with original lineage.
    let request = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(321)).await;
    assert!(matches!(event(&mut connection).await,
        Event::Audio {lineage, pcm, ..} if lineage.request == request && lineage.session.get() == 17 && pcm == [321]));
    send_json(
        &mut server,
        json!({"sessionResumptionUpdate":{"newHandle":"","resumable":false}}),
    )
    .await;
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}}),
    )
    .await;
    assert!(matches!(event(&mut connection).await,
        Event::TurnComplete {lineage, idle:true} if lineage.request == request));
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn empty_messages_between_transcript_and_audio_preserve_response_lifecycle() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    send_json(
        &mut server,
        json!({"serverContent":{"inputTranscription":{"text":"How are you doing today?"}}}),
    )
    .await;
    send_json(&mut server, json!({})).await;
    send_json(&mut server, json!({"serverContent":{}})).await;
    send_json(&mut server, audio(321)).await;
    assert!(matches!(event(&mut connection).await,
        Event::UncorrelatedInputTranscript { session, text, finished:false }
        if session.get() == 17 && text == "How are you doing today?"));
    assert!(matches!(event(&mut connection).await,
        Event::Audio { lineage, sequence:0, pcm }
        if lineage.request == request && lineage.session.get() == 17 && pcm == [321]));
    send_json(&mut server, json!({})).await;
    send_json(&mut server, audio(-123)).await;
    assert!(matches!(event(&mut connection).await,
        Event::Audio { lineage, sequence:1, pcm }
        if lineage.request == request && lineage.session.get() == 17 && pcm == [-123]));
    send_json(&mut server, json!({})).await;
    send_json(
        &mut server,
        json!({"serverContent":{"generationComplete":true}}),
    )
    .await;
    assert!(matches!(event(&mut connection).await,
        Event::GenerationComplete { lineage } if lineage.request == request));
    send_json(&mut server, json!({})).await;
    assert!(
        timeout(Duration::from_millis(30), connection.next_event())
            .await
            .is_err()
    );
    assert_eq!(connection.state(), State::Ready);
    send_json(
        &mut server,
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}}),
    )
    .await;
    assert!(matches!(event(&mut connection).await,
        Event::TurnComplete { lineage, idle:true } if lineage.request == request));
    assert_eq!(connection.state(), State::Ready);
}
