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
    mock_with(configuration(timeouts)).await
}
async fn mock_with(configuration: SessionConfig) -> (Connection, MockSocket) {
    let (client, mut server) = pair(MAX_WIRE_BYTES * 2).await;
    let connect = tokio::spawn(setup_and_spawn(
        client,
        configuration,
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
fn idle() -> Value {
    json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}})
}
fn generated() -> Value {
    json!({"serverContent":{"generationComplete":true}})
}
fn request(id: u64) -> RequestId {
    RequestId::new(id).unwrap()
}
async fn quiet(connection: &mut Connection) {
    assert!(
        timeout(Duration::from_millis(40), connection.next_event())
            .await
            .is_err()
    );
}
async fn silent(server: &mut MockSocket) {
    assert!(
        timeout(Duration::from_millis(40), server.next())
            .await
            .is_err()
    );
}
async fn sent(server: &mut MockSocket, field: &str) {
    let message = receive(server).await;
    assert!(
        message["realtimeInput"].get(field).is_some(),
        "expected {field}, received {message}"
    );
}
async fn discarded(connection: &mut Connection, expected: Discard) {
    assert!(matches!(
        event(connection).await,
        Event::Discarded { session, reason } if session.get() == 17 && reason == expected
    ));
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
    // Nothing owns output after the idle barrier. It is dropped and reported;
    // it cannot end a healthy session or reach a later request.
    send_json(&mut server, audio(8)).await;
    discarded(&mut connection, Discard::UnownedOutput).await;
    send_json(&mut server, idle()).await;
    discarded(&mut connection, Discard::UnownedOutput).await;
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn interruption_streams_new_audio_at_once_and_holds_only_its_end_until_idle_barrier() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    let new = request(2);
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
    sent(&mut server, "activityStart").await;
    // The opening words reach the service before any barrier arrives.
    sent(&mut server, "audio").await;
    assert!(
        matches!(event(&mut connection).await, Event::InputStarted { lineage, waiting_for_barrier:true } if lineage.request == new)
    );
    silent(&mut server).await;
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
    silent(&mut server).await;
    send_json(&mut server, audio(112)).await;
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle:true } if lineage.request == old)
    );
    // Only now may the service begin the new response.
    sent(&mut server, "activityEnd").await;
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
async fn stalled_consumer_stops_socket_reads_then_fails_with_received_output_intact() {
    let timeouts = Timeouts {
        deliver: Duration::from_millis(60),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    for value in 0..EVENT_QUEUE_CAPACITY as i16 + 8 {
        send_json(&mut server, audio(value)).await;
    }
    assert_eq!(disconnected(&connection).await, Error::Backpressure);
    // The queue is full, one message waited in the outbox and the rest were
    // never read from the socket: nothing grew with the traffic.
    assert_eq!(connection.events.len(), EVENT_QUEUE_CAPACITY);
    for value in 0..EVENT_QUEUE_CAPACITY as i16 {
        assert!(
            matches!(event(&mut connection).await, Event::Audio { sequence, pcm, .. } if sequence == value as u64 && pcm == [value])
        );
    }
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
async fn slow_barrier_neither_buffers_nor_ages_the_new_request() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    let new = request(2);
    let input = connection.input();
    input.try_start(new).unwrap();
    sent(&mut server, "activityStart").await;
    event(&mut connection).await;
    // More than a second of speech while the old response is still unbarred.
    // Each block is on the wire before the next is offered; none is retained.
    for sequence in 0..110 {
        input
            .try_audio(
                new,
                sequence,
                Instant::now() - Duration::from_millis(900),
                &[3; 160],
            )
            .unwrap();
        sent(&mut server, "audio").await;
    }
    input.try_end(new).unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(connection.state(), State::Ready);
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    sent(&mut server, "activityEnd").await;
    send_json(&mut server, audio(5)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, .. } if lineage.request == new)
    );
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

// ---- Complete delivery -------------------------------------------------

#[tokio::test]
async fn burst_released_after_a_network_stall_is_delivered_completely_and_in_order() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    // Everything is on the socket before the consumer takes one event: far
    // more than the event queue holds, as after a stalled link recovers.
    const CHUNKS: i16 = 400;
    for value in 0..CHUNKS {
        server
            .feed(Message::text(audio(value).to_string()))
            .await
            .unwrap();
    }
    server
        .feed(Message::text(generated().to_string()))
        .await
        .unwrap();
    server
        .feed(Message::text(idle().to_string()))
        .await
        .unwrap();
    server.flush().await.unwrap();
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert_eq!(connection.state(), State::Ready);
    assert!(connection.events.len() <= EVENT_QUEUE_CAPACITY);
    for value in 0..CHUNKS {
        assert!(
            matches!(event(&mut connection).await, Event::Audio { lineage, sequence, pcm } if lineage.request == request && sequence == value as u64 && pcm == [value])
        );
    }
    assert!(matches!(
        event(&mut connection).await,
        Event::GenerationComplete { .. }
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn slow_but_progressing_consumer_is_not_mistaken_for_a_stalled_one() {
    let timeouts = Timeouts {
        deliver: Duration::from_millis(120),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    const CHUNKS: i16 = 48;
    for value in 0..CHUNKS {
        server
            .feed(Message::text(audio(value).to_string()))
            .await
            .unwrap();
    }
    server.flush().await.unwrap();
    // Delivery stays backed up for several times the bound, but an event is
    // taken well inside it each time.
    for value in 0..CHUNKS {
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(
            matches!(event(&mut connection).await, Event::Audio { sequence, .. } if sequence == value as u64)
        );
    }
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn commands_and_local_retirement_stay_live_while_output_delivery_waits() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    for value in 0..EVENT_QUEUE_CAPACITY as i16 + 4 {
        send_json(&mut server, audio(value)).await;
    }
    tokio::time::sleep(Duration::from_millis(40)).await;
    // The consumer has taken nothing, so cloud output is backed up. A new
    // request must still reach the service and retire the old one at once.
    let new = request(2);
    let input = connection.input();
    input.try_start(new).unwrap();
    input.try_audio(new, 0, Instant::now(), &[4; 160]).unwrap();
    sent(&mut server, "activityStart").await;
    sent(&mut server, "audio").await;
    // Every queued block of the retired answer is skipped by the receiver.
    assert!(
        matches!(event(&mut connection).await, Event::InputStarted { lineage, .. } if lineage.request == new)
    );
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle: true } if lineage.request == old)
    );
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn output_received_before_a_disconnect_is_delivered_before_the_stream_ends() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    send_json(&mut server, audio(2)).await;
    send_json(&mut server, generated()).await;
    send_json(&mut server, idle()).await;
    server
        .close(Some(tungstenite::protocol::CloseFrame {
            code: 1008.into(),
            reason: "PRIVATE IDLE REASON".into(),
        }))
        .await
        .unwrap();
    // The failure is already known when the consumer first looks.
    assert_eq!(
        disconnected(&connection).await,
        Error::PeerClosed { code: Some(1008) }
    );
    for expected in [1, 2] {
        assert!(
            matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == request && pcm == [expected])
        );
    }
    assert!(matches!(
        event(&mut connection).await,
        Event::GenerationComplete { .. }
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    assert!(connection.next_event().await.is_none());
}

#[tokio::test]
async fn local_shutdown_still_drops_queued_output() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    send_json(&mut server, generated()).await;
    timeout(Duration::from_secs(2), async {
        while connection.events.len() < 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    connection.shutdown();
    assert!(matches!(
        event(&mut connection).await,
        Event::GenerationComplete { .. }
    ));
    assert!(connection.next_event().await.is_none());
    assert_eq!(connection.state(), State::Closed);
}

#[tokio::test]
async fn a_progressing_answer_outlives_the_first_response_deadline() {
    let timeouts = Timeouts {
        response: Duration::from_millis(150),
        stall: Duration::from_millis(600),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    // Delayed chunks spanning three times the first-response bound.
    for value in 0..9 {
        tokio::time::sleep(Duration::from_millis(50)).await;
        send_json(&mut server, audio(value)).await;
        assert!(
            matches!(event(&mut connection).await, Event::Audio { sequence, .. } if sequence == value as u64)
        );
    }
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn idle_completion_is_awaited_for_the_assumed_playback_of_the_answer() {
    let timeouts = Timeouts {
        stall: Duration::from_millis(60),
        completion: Duration::from_millis(100),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    // 0.5 s of generated speech, complete at once. The service may withhold
    // the idle completion for that long; the stall bound no longer applies.
    let half_second = STANDARD.encode(vec![0u8; 24_000]);
    send_json(&mut server, json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000","data":half_second}}]}}})).await;
    send_json(&mut server, generated()).await;
    event(&mut connection).await;
    event(&mut connection).await;
    tokio::time::sleep(Duration::from_millis(350)).await;
    assert_eq!(connection.state(), State::Ready);
    // No completion after playback plus the allowance is a failure, reported
    // as such rather than as a silent hang.
    assert_eq!(disconnected(&connection).await, Error::CompletionTimeout);
}

#[tokio::test]
async fn silence_before_and_during_an_answer_have_separate_bounded_failures() {
    let timeouts = Timeouts {
        response: Duration::from_millis(60),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    assert_eq!(disconnected(&connection).await, Error::ResponseTimeout);

    let timeouts = Timeouts {
        stall: Duration::from_millis(60),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    assert_eq!(disconnected(&connection).await, Error::StalledResponse);
    // What did arrive is not withheld by the failure.
    assert!(matches!(event(&mut connection).await, Event::Audio { .. }));
    assert!(connection.next_event().await.is_none());

    let timeouts = Timeouts {
        turn: Duration::from_millis(120),
        stall: Duration::from_secs(5),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    assert_eq!(disconnected(&connection).await, Error::ResponseTimeout);
}

// ---- Follow-ups, interruptions and topic changes -----------------------

#[tokio::test]
async fn second_interruption_before_the_barrier_replaces_the_waiting_request() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    event(&mut connection).await;
    let input = connection.input();
    let (second, third) = (request(2), request(3));
    input.try_start(second).unwrap();
    input
        .try_audio(second, 0, Instant::now(), &[2; 160])
        .unwrap();
    input.try_end(second).unwrap();
    sent(&mut server, "activityStart").await;
    sent(&mut server, "audio").await;
    event(&mut connection).await;
    // The person keeps talking before the old response is barred.
    input.try_start(third).unwrap();
    input
        .try_audio(third, 0, Instant::now(), &[3; 160])
        .unwrap();
    sent(&mut server, "audio").await;
    assert!(
        matches!(event(&mut connection).await, Event::InputStarted { lineage, waiting_for_barrier: true } if lineage.request == third)
    );
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, .. } if lineage.request.get() == 1)
    );
    // The barrier has arrived but the newest input is still open: no End yet,
    // and never one for the request it replaced.
    silent(&mut server).await;
    input.try_end(third).unwrap();
    sent(&mut server, "activityEnd").await;
    send_json(&mut server, audio(33)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == third && pcm == [33])
    );
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn input_cancelled_before_its_end_never_asks_for_a_response() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let input = connection.input();
    let (first, second) = (request(1), request(2));
    input.try_start(first).unwrap();
    sent(&mut server, "activityStart").await;
    event(&mut connection).await;
    input
        .try_audio(first, 0, Instant::now(), &[1; 160])
        .unwrap();
    sent(&mut server, "audio").await;
    input.retire(first);
    input.try_end(first).unwrap();
    // No activityEnd: the service is not asked to answer cancelled input.
    silent(&mut server).await;
    input.try_start(second).unwrap();
    assert!(
        matches!(event(&mut connection).await, Event::InputStarted { lineage, waiting_for_barrier: false } if lineage.request == second)
    );
    // The activity is still open, so no second interruption request is sent.
    silent(&mut server).await;
    input
        .try_audio(second, 0, Instant::now(), &[2; 160])
        .unwrap();
    sent(&mut server, "audio").await;
    input.try_end(second).unwrap();
    sent(&mut server, "activityEnd").await;
    send_json(&mut server, audio(7)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == second && pcm == [7])
    );
}

#[tokio::test]
async fn waiting_request_cancelled_before_the_barrier_is_never_committed() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    let input = connection.input();
    let second = request(2);
    input.try_start(second).unwrap();
    input
        .try_audio(second, 0, Instant::now(), &[2; 160])
        .unwrap();
    input.try_end(second).unwrap();
    sent(&mut server, "activityStart").await;
    sent(&mut server, "audio").await;
    event(&mut connection).await;
    input.retire(second);
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    silent(&mut server).await;
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn unanswered_interruption_fails_by_default_and_can_assume_cancellation() {
    let timeouts = Timeouts {
        barrier: Duration::from_millis(80),
        ..Timeouts::default()
    };
    // The first request ended but the service has sent nothing for it when
    // the person resumes speaking, and it sends no terminal event either.
    for policy in [
        UnansweredInterruption::Fail,
        UnansweredInterruption::AssumeCancelled,
    ] {
        let (mut connection, mut server) =
            mock_with(configuration(timeouts).unanswered_interruption(policy)).await;
        ended_turn(&mut connection, &mut server, 1).await;
        let second = request(2);
        let input = connection.input();
        input.try_start(second).unwrap();
        input
            .try_audio(second, 0, Instant::now(), &[2; 160])
            .unwrap();
        input.try_end(second).unwrap();
        sent(&mut server, "activityStart").await;
        sent(&mut server, "audio").await;
        event(&mut connection).await;
        if policy == UnansweredInterruption::Fail {
            assert_eq!(disconnected(&connection).await, Error::BarrierTimeout);
            continue;
        }
        discarded(&mut connection, Discard::UnansweredBarrier).await;
        sent(&mut server, "activityEnd").await;
        send_json(&mut server, audio(9)).await;
        assert!(
            matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == second && pcm == [9])
        );
        assert_eq!(connection.state(), State::Ready);
    }
    // Once the old response has produced anything, the barrier is mandatory
    // under either policy.
    let (mut connection, mut server) = mock_with(
        configuration(timeouts).unanswered_interruption(UnansweredInterruption::AssumeCancelled),
    )
    .await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    event(&mut connection).await;
    connection.input().try_start(request(2)).unwrap();
    assert_eq!(disconnected(&connection).await, Error::BarrierTimeout);
}

// ---- Late events from an old request ------------------------------------

#[tokio::test]
async fn late_interruption_and_completion_never_cancel_a_request_still_being_spoken() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(5)).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    event(&mut connection).await;
    let new = request(2);
    let input = connection.input();
    input.try_start(new).unwrap();
    sent(&mut server, "activityStart").await;
    event(&mut connection).await;
    // The explicit activityStart above asked for an interruption. Its delayed
    // result and a duplicate completion arrive while the person is speaking.
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    discarded(&mut connection, Discard::LateInterruption).await;
    send_json(&mut server, idle()).await;
    discarded(&mut connection, Discard::LateTerminal).await;
    send_json(&mut server, audio(66)).await;
    discarded(&mut connection, Discard::LateOutput).await;
    send_json(
        &mut server,
        json!({"serverContent":{"interrupted":true,"turnComplete":true,"interactionStatus":"IDLE"}}),
    )
    .await;
    discarded(&mut connection, Discard::LateInterruption).await;
    // The new request is intact: still accepted, ended once, answered once.
    input.try_audio(new, 0, Instant::now(), &[2; 160]).unwrap();
    sent(&mut server, "audio").await;
    input.try_end(new).unwrap();
    sent(&mut server, "activityEnd").await;
    send_json(&mut server, audio(9)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, sequence: 0, pcm } if lineage.request == new && pcm == [9])
    );
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle: true } if lineage.request == new)
    );
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn late_terminal_pair_after_input_end_does_not_complete_the_new_request_empty() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    let new = ended_turn(&mut connection, &mut server, 2).await;
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    discarded(&mut connection, Discard::LateInterruption).await;
    send_json(&mut server, idle()).await;
    discarded(&mut connection, Discard::LateTerminal).await;
    send_json(&mut server, audio(9)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == new && pcm == [9])
    );
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle: true } if lineage.request == new)
    );
}

#[tokio::test]
async fn an_old_stray_interruption_cannot_swallow_a_later_empty_completion() {
    let timeouts = Timeouts {
        barrier: Duration::from_millis(60),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    let new = ended_turn(&mut connection, &mut server, 2).await;
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    discarded(&mut connection, Discard::LateInterruption).await;
    // No companion followed within the barrier window. A completion this late
    // is the service declining to answer, and is reported as such.
    tokio::time::sleep(Duration::from_millis(120)).await;
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle: true } if lineage.request == new)
    );
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test]
async fn unrequested_interruption_cannot_cut_an_answer_in_progress() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    event(&mut connection).await;
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    discarded(&mut connection, Discard::LateInterruption).await;
    // Had the request been retired, the receiver would drop this audio.
    send_json(&mut server, audio(2)).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, sequence: 1, pcm } if lineage.request == request && pcm == [2])
    );
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::TurnComplete { lineage, idle: true } if lineage.request == request)
    );
}

#[tokio::test]
async fn trailing_output_transcript_stays_with_the_request_that_spoke_it() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    event(&mut connection).await;
    // The service orders transcripts independently of audio and completion.
    send_json(
        &mut server,
        json!({"serverContent":{"outputTranscription":{"text":"old words"}}}),
    )
    .await;
    assert!(
        matches!(event(&mut connection).await, Event::OutputTranscript { lineage, text, .. } if lineage.request == old && text == "old words")
    );
    let new = ended_turn(&mut connection, &mut server, 2).await;
    // Still the old answer's words: the new request has produced nothing, and
    // the old one is retired, so they are not delivered under either name.
    send_json(
        &mut server,
        json!({"serverContent":{"outputTranscription":{"text":"more old words","finished":true}}}),
    )
    .await;
    quiet(&mut connection).await;
    let mut first = audio(2);
    first["serverContent"]["outputTranscription"] = json!({"text":"new words"});
    send_json(&mut server, first).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, .. } if lineage.request == new)
    );
    assert!(
        matches!(event(&mut connection).await, Event::OutputTranscript { lineage, text, .. } if lineage.request == new && text == "new words")
    );
    assert_eq!(connection.state(), State::Ready);
}

// ---- Disconnects, quota and context -----------------------------------

#[tokio::test]
async fn go_away_ends_an_idle_connection_and_lets_an_unfinished_answer_complete() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    send_json(&mut server, json!({"goAway":{"timeLeft":"5s"}})).await;
    assert!(
        matches!(event(&mut connection).await, Event::GoAway { session, time_left: Some(left) } if session.get() == 17 && left == Duration::from_secs(5))
    );
    assert!(connection.next_event().await.is_none());
    assert_eq!(connection.state(), State::Disconnected(Error::ServerGoAway));

    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    send_json(&mut server, audio(1)).await;
    send_json(&mut server, json!({"goAway":{}})).await;
    assert!(matches!(event(&mut connection).await, Event::Audio { .. }));
    assert!(matches!(
        event(&mut connection).await,
        Event::GoAway {
            time_left: None,
            ..
        }
    ));
    quiet(&mut connection).await;
    assert_eq!(connection.state(), State::Ready);
    send_json(&mut server, audio(2)).await;
    send_json(&mut server, generated()).await;
    send_json(&mut server, idle()).await;
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, pcm, .. } if lineage.request == request && pcm == [2])
    );
    assert!(matches!(
        event(&mut connection).await,
        Event::GenerationComplete { .. }
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { idle: true, .. }
    ));
    assert!(connection.next_event().await.is_none());
    assert_eq!(connection.state(), State::Disconnected(Error::ServerGoAway));
}

#[tokio::test]
async fn quota_refusals_get_a_fixed_category_and_no_text_is_retained() {
    let (connection, mut server) = mock(Timeouts::default()).await;
    send_json(
        &mut server,
        json!({"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"PRIVATE QUOTA DETAIL"}}),
    )
    .await;
    let error = disconnected(&connection).await;
    assert_eq!(error, Error::QuotaExceeded);
    assert_eq!(error.recovery(), crate::Recovery::Unavailable);
    assert!(!format!("{error:?} {error}").contains("PRIVATE"));

    let (connection, mut server) = mock(Timeouts::default()).await;
    server
        .close(Some(tungstenite::protocol::CloseFrame {
            code: 1011.into(),
            reason: "You exceeded your current quota. PRIVATE ACCOUNT DETAIL".into(),
        }))
        .await
        .unwrap();
    let error = disconnected(&connection).await;
    assert_eq!(error, Error::QuotaExceeded);
    assert!(!format!("{:?}", connection.state()).contains("PRIVATE"));

    let (connection, mut server) = mock(Timeouts::default()).await;
    send_json(
        &mut server,
        json!({"error":{"code":503,"status":"UNAVAILABLE"}}),
    )
    .await;
    let error = disconnected(&connection).await;
    assert_eq!(error, Error::ServerUnavailable);
    assert_eq!(error.recovery(), crate::Recovery::Reconnect);
    // Application close codes used by the deployment relay for refusals.
    assert_eq!(
        Error::PeerClosed { code: Some(4029) }.recovery(),
        crate::Recovery::Unavailable
    );
    assert_eq!(
        Error::PeerClosed { code: Some(1008) }.recovery(),
        crate::Recovery::Reconnect
    );
    assert_eq!(Error::Authentication.recovery(), crate::Recovery::Fatal);
}

#[tokio::test]
async fn resumption_handle_is_kept_only_when_configured_and_never_printed() {
    let update =
        json!({"sessionResumptionUpdate":{"newHandle":"PRIVATE-HANDLE-1","resumable":true}});
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    send_json(&mut server, update.clone()).await;
    send_json(
        &mut server,
        json!({"voiceActivity":{"type":"ACTIVITY_END"}}),
    )
    .await;
    event(&mut connection).await;
    assert!(connection.resumption().is_none());

    let (mut connection, mut server) =
        mock_with(configuration(Timeouts::default()).resumption(Resumption::Retain)).await;
    send_json(&mut server, update).await;
    send_json(
        &mut server,
        json!({"voiceActivity":{"type":"ACTIVITY_END"}}),
    )
    .await;
    event(&mut connection).await;
    let point = connection.resumption().unwrap();
    assert!(point.is_current());
    assert!(!format!("{point:?}").contains("PRIVATE-HANDLE"));
    // A later point that cannot be resumed keeps the earlier handle, marked
    // as no longer covering the whole conversation.
    send_json(
        &mut server,
        json!({"sessionResumptionUpdate":{"newHandle":"","resumable":false}}),
    )
    .await;
    send_json(
        &mut server,
        json!({"voiceActivity":{"type":"ACTIVITY_END"}}),
    )
    .await;
    event(&mut connection).await;
    assert!(!connection.resumption().unwrap().is_current());
    // The point outlives the connection so a reconnect can present it.
    connection.shutdown();
    assert!(connection.next_event().await.is_none());
    assert!(connection.resumption().is_some());
}
