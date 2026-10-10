use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, CaptureState, Controller, MonoTime, Permission,
    TurnOwner,
};
use lamp_live::{
    fixture_provider::{
        CUE_MAX_BYTES, CueSink, Fixture, FixtureOptions, FixtureProvider, MAX_REPLY_FRAMES,
    },
    process::{SessionDirectory, Worker},
    provider_worker::{ProviderInput, ProviderOutput},
    transport::WorkerChannels,
    wire::Control,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, DirBuilder},
    io::{self, Cursor},
    os::unix::{
        fs::{DirBuilderExt, PermissionsExt, symlink},
        net::UnixDatagram,
    },
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
const AT: u64 = 10_000_000;
fn time(at: u64) -> MonoTime {
    MonoTime::from_micros(at)
}
fn boot() -> BootId {
    BootId::new([7; 16]).unwrap()
}
struct Private {
    path: PathBuf,
}
impl Private {
    fn new() -> Self {
        let path = PathBuf::from("/tmp").join(format!(
            "lamp-fixture-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self { path }
    }
    fn wave(&self, rate: u32, channels: u16, frames: usize) -> (PathBuf, String, Vec<i16>) {
        let samples: Vec<_> = (0..frames).map(|i| (i % 16) as i16 * 100 - 700).collect();
        let path = self.path.join(format!("{rate}-{channels}-{frames}.wav"));
        let mut bytes = Cursor::new(Vec::new());
        {
            let mut writer = hound::WavWriter::new(
                &mut bytes,
                hound::WavSpec {
                    channels,
                    sample_rate: rate,
                    bits_per_sample: 16,
                    sample_format: hound::SampleFormat::Int,
                },
            )
            .unwrap();
            for sample in &samples {
                writer.write_sample(*sample).unwrap();
            }
            writer.finalize().unwrap();
        }
        let bytes = bytes.into_inner();
        fs::write(&path, &bytes).unwrap();
        (path, format!("{:x}", Sha256::digest(&bytes)), samples)
    }
    fn fixture(&self) -> Fixture {
        let (path, hash, _) = self.wave(24_000, 1, 2500);
        Fixture::load(&path, &hash).unwrap()
    }
}
impl Drop for Private {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

struct Rig {
    owner: Controller,
    engine: FixtureProvider,
    now: u64,
    privacy: u64,
    turn: TurnOwner,
}
impl Rig {
    fn new(private: &Private) -> Self {
        let mut owner = Controller::new(boot(), time(AT));
        owner
            .set_microphone_permission(time(AT), Permission::Allowed)
            .unwrap();
        owner
            .set_capture(time(AT), CaptureState::RetainingUntil(time(AT + 900_000)))
            .unwrap();
        owner
            .set_admission(time(AT), AdmissionState::OpenUntil(time(AT + 900_000)))
            .unwrap();
        let turn = owner.admit(time(AT), AdmittedInput::NewTurn).unwrap();
        let state = owner.snapshot(time(AT)).unwrap();
        let mut engine = FixtureProvider::new(private.fixture(), boot(), AT);
        assert!(engine.install(AT, state).unwrap());
        Self {
            owner,
            engine,
            now: AT,
            privacy: state.microphone_generation(),
            turn,
        }
    }
    fn command(&mut self, command: ProviderInput) {
        self.now += 1;
        self.engine.submit(self.now, command).unwrap();
    }
    fn start(&mut self) {
        self.command(ProviderInput::Start {
            request: self.turn.turn(),
            privacy_generation: self.privacy,
        });
    }
    fn audio(&mut self) {
        self.command(ProviderInput::Audio {
            request: self.turn.turn(),
            privacy_generation: self.privacy,
            sequence: 1,
            read_completed_at_us: self.now,
            samples: vec![15; 160],
        });
    }
    fn end(&mut self) {
        self.now += 1;
        self.owner.input_ended(time(self.now), self.turn).unwrap();
        assert!(
            self.engine
                .install(self.now, self.owner.snapshot(time(self.now)).unwrap())
                .unwrap()
        );
        self.command(ProviderInput::End {
            request: self.turn.turn(),
            privacy_generation: self.privacy,
        });
    }
    fn drain(&mut self) -> Vec<Value> {
        let mut out = Vec::new();
        for _ in 0..100 {
            self.now += 1;
            if !self
                .engine
                .send_one(self.now, |event| {
                    out.push(serde_json::to_value(event).unwrap());
                    Ok(())
                })
                .unwrap()
            {
                return out;
            }
        }
        panic!("fixture output was not finite");
    }
    fn replace(&mut self) {
        self.now += 1;
        self.turn = self
            .owner
            .admit(time(self.now), AdmittedInput::Interruption)
            .unwrap();
        assert!(
            self.engine
                .install(self.now, self.owner.snapshot(time(self.now)).unwrap())
                .unwrap()
        );
    }
}

#[test]
fn loader_keeps_exact_pcm_and_hash_and_rejects_mismatch_format_size_and_symlink() {
    let private = Private::new();
    let (path, hash, samples) = private.wave(24_000, 1, 2500);
    let fixture = Fixture::load(&path, &hash).unwrap();
    assert_eq!(fixture.info.wav_sha256, hash);
    assert_eq!(fixture.info.frames, samples.len());
    assert_eq!(fixture.info.rail_samples, 0);
    assert!(Fixture::load(&path, &"0".repeat(64)).is_err());
    assert!(Fixture::load(&path, "short").is_err());
    for (rate, channels, frames) in [
        (16_000, 1, 1000),
        (24_000, 2, 1000),
        (24_000, 1, 239),
        (24_000, 1, MAX_REPLY_FRAMES + 1),
    ] {
        let (bad, hash, _) = private.wave(rate, channels, frames);
        assert!(Fixture::load(&bad, &hash).is_err());
    }
    let link = private.path.join("link.wav");
    symlink(&path, &link).unwrap();
    assert!(Fixture::load(&link, &hash).is_err());
    assert!(Fixture::load(&private.path, &hash).is_err());
    let mut bytes = fs::read(&path).unwrap();
    bytes.pop();
    let truncated_hash = format!("{:x}", Sha256::digest(&bytes));
    fs::write(&path, bytes).unwrap();
    assert!(Fixture::load(&path, &truncated_hash).is_err());
}

#[test]
fn one_admitted_completed_input_gets_exact_reply_and_no_repeat() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.start();
    rig.audio();
    rig.end();
    let output = rig.drain();
    let received: Vec<i16> = output
        .iter()
        .filter(|e| e["kind"] == "audio")
        .flat_map(|e| {
            e["samples"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s.as_i64().unwrap() as i16)
        })
        .collect();
    assert_eq!(
        received,
        (0..2500)
            .map(|i| (i % 16) as i16 * 100 - 700)
            .collect::<Vec<_>>()
    );
    assert!(
        output
            .iter()
            .filter(|e| e["kind"] == "audio")
            .all(|e| e["request"] == 1 && e["samples"].as_array().unwrap().len() <= 960)
    );
    assert_eq!(output.last().unwrap()["kind"], "turn_complete");
    rig.replace();
    rig.start();
    rig.audio();
    rig.end();
    let next = rig.drain();
    assert!(!next.iter().any(|event| event["kind"] == "audio"));
    assert_eq!(next.last().unwrap()["request"], 2);
    assert_eq!(next.last().unwrap()["kind"], "turn_complete");
}

#[test]
fn no_audio_before_real_endpoint_or_without_admitted_authority() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.start();
    rig.audio();
    assert!(!rig.drain().iter().any(|e| e["kind"] == "audio"));
    let mut engine = FixtureProvider::new(private.fixture(), boot(), AT);
    assert!(
        engine
            .submit(
                AT,
                ProviderInput::Start {
                    request: 1,
                    privacy_generation: 1
                }
            )
            .is_err()
    );
    let mut rig = Rig::new(&private);
    rig.start();
    assert!(
        rig.engine
            .submit(
                AT + 2,
                ProviderInput::End {
                    request: 1,
                    privacy_generation: rig.privacy
                }
            )
            .is_err()
    );
}

#[test]
fn cancellation_overtaking_start_reserves_original_owner_and_never_relabels_reply() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    let old_privacy = rig.privacy;
    rig.replace();
    rig.command(ProviderInput::Start {
        request: 1,
        privacy_generation: old_privacy,
    });
    rig.start();
    rig.audio();
    rig.end();
    assert!(!rig.drain().iter().any(|e| e["kind"] == "audio"));
}

#[test]
fn blocked_output_is_retained_then_discarded_on_new_owner_without_replay() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.start();
    rig.audio();
    rig.end();
    let mut saw_audio = false;
    for _ in 0..10 {
        rig.now += 1;
        let sent = rig
            .engine
            .send_one(rig.now, |event| {
                if matches!(event, ProviderOutput::Audio { .. }) {
                    saw_audio = true;
                    Err(io::ErrorKind::WouldBlock.into())
                } else {
                    Ok(())
                }
            })
            .unwrap();
        if !sent {
            break;
        }
    }
    assert!(saw_audio);
    rig.replace();
    rig.start();
    rig.audio();
    rig.end();
    assert!(!rig.drain().iter().any(|e| e["kind"] == "audio"));
}

#[test]
fn cancellation_overtaking_end_drops_queued_pcm() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.start();
    rig.audio();
    rig.replace();
    rig.command(ProviderInput::End {
        request: 1,
        privacy_generation: rig.privacy,
    });
    assert!(!rig.drain().iter().any(|e| e["kind"] == "audio"));
}

#[test]
fn privacy_close_and_coalesced_close_reopen_stop_without_revival() {
    for reopen in [false, true] {
        let private = Private::new();
        let mut rig = Rig::new(&private);
        rig.start();
        rig.audio();
        rig.end();
        rig.now += 1;
        rig.owner
            .set_microphone_permission(time(rig.now), Permission::Denied)
            .unwrap();
        if reopen {
            rig.owner
                .set_microphone_permission(time(rig.now), Permission::Allowed)
                .unwrap();
        }
        assert!(
            !rig.engine
                .install(rig.now, rig.owner.snapshot(time(rig.now)).unwrap())
                .unwrap()
        );
        assert!(
            rig.engine
                .send_one(rig.now, |_| panic!("private PCM escaped"))
                .is_err()
        );
    }
}

#[test]
fn expired_lease_and_regressing_clock_latch_failure_before_output() {
    for expired in [true, false] {
        let private = Private::new();
        let mut rig = Rig::new(&private);
        rig.start();
        rig.audio();
        rig.end();
        let check = if expired { rig.now + 250_000 } else { AT - 1 };
        assert!(
            rig.engine
                .send_one(check, |_| panic!("stale PCM escaped"))
                .is_err()
        );
        assert!(
            rig.engine
                .send_one(AT + 250_001, |_| panic!("fault revived"))
                .is_err()
        );
    }
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.now += 1;
    rig.owner
        .set_capture(
            time(rig.now),
            CaptureState::RetainingUntil(time(rig.now + 10)),
        )
        .unwrap();
    rig.engine
        .install(rig.now, rig.owner.snapshot(time(rig.now)).unwrap())
        .unwrap();
    assert!(
        rig.engine
            .send_one(rig.now + 10, |_| panic!("expired input lease escaped"))
            .is_err()
    );
}

#[test]
fn bad_audio_order_privacy_shape_and_time_fail_closed() {
    for (sequence, privacy_delta, read_time, len) in [
        (2, 0, AT, 160),
        (1, 1, AT, 160),
        (1, 0, AT + 50, 160),
        (1, 0, AT - 1_000_000, 160),
        (1, 0, AT, 159),
    ] {
        let private = Private::new();
        let mut rig = Rig::new(&private);
        rig.start();
        assert!(
            rig.engine
                .submit(
                    AT + 2,
                    ProviderInput::Audio {
                        request: 1,
                        privacy_generation: rig.privacy + privacy_delta,
                        sequence,
                        read_completed_at_us: read_time,
                        samples: vec![0; len]
                    }
                )
                .is_err()
        );
        assert!(
            rig.engine
                .send_one(AT + 3, |_| panic!("failed input revived"))
                .is_err()
        );
    }
}

#[test]
fn fixture_cli_requires_explicit_retention_and_no_unknown_or_duplicate_flags() {
    let parse = |args: &[&str]| {
        FixtureOptions::parse(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>())
    };
    assert!(parse(&[]).is_err());
    assert!(
        parse(&[
            "--diagnostics",
            "--noise-suppression",
            "off",
            "--cue-socket",
            "/tmp/cue"
        ])
        .unwrap()
        .cue_socket
        .is_some()
    );
    for args in [
        vec!["--diagnostics", "--cue-socket"],
        vec!["--diagnostics", "--cue-socket", "a", "--cue-socket", "b"],
        vec!["--diagnostics", "--noise-suppression", "yes"],
        vec!["--diagnostics", "--unknown"],
    ] {
        assert!(parse(&args).is_err());
    }
}

fn bound_cue(private: &Private) -> (UnixDatagram, CueSink) {
    let path = private.path.join("cue");
    let peer = UnixDatagram::bind(&path).unwrap();
    peer.set_nonblocking(true).unwrap();
    let mut sink = CueSink::connect(&path).unwrap();
    sink.set_boot(boot());
    (peer, sink)
}
#[test]
fn cues_are_bounded_ordered_fresh_and_keep_owner_epoch_context() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    let trace = vec![
        json!({"kind":"capture_started","details":{"epoch":3}}),
        json!({"kind":"reference_clock","details":{"playback_epoch":8}}),
        json!({"kind":"listening_ready","at_us":AT}),
        json!({"kind":"speaker_first_write","at_us":AT+1,"turn":9,"owner":{"generation":11},"playback_sequence":17}),
    ];
    assert_eq!(sink.scan(&trace, AT + 2), None);
    let mut bytes = [0; CUE_MAX_BYTES];
    for (sequence, kind) in [(1, "listening_ready"), (2, "speaker_first_write")] {
        let count = peer.recv(&mut bytes).unwrap();
        assert!(count <= CUE_MAX_BYTES);
        let event: Value = serde_json::from_slice(&bytes[..count]).unwrap();
        assert_eq!(event["sequence"], sequence);
        assert_eq!(event["kind"], kind);
        assert_eq!(event["capture_epoch"], 3);
        assert_eq!(event["reference_epoch_context"], 8);
        assert_eq!(event["sent_us"], AT + 2);
        assert!(event["expires_us"].as_u64().unwrap() > AT + 2);
        if sequence == 2 {
            assert_eq!(event["turn"], 9);
            assert_eq!(event["generation"], 11);
            assert_eq!(event["playback_sequence"], 17);
        }
    }
    assert_eq!(sink.scan(&trace, AT + 3), None);
    assert_eq!(
        peer.recv(&mut bytes).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
}

#[test]
fn conversation_cues_preserve_turns_endpoints_and_cancellation_reasons() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    let trace = vec![
        json!({"kind":"input_admitted","at_us":AT,"turn":9,"owner":{"generation":11}}),
        json!({"kind":"local_endpoint","at_us":AT+1,"turn":9,"owner":{"generation":11}}),
        json!({"kind":"turn_finished","at_us":AT+2,"turn":9,"owner":{"generation":11},"outcome":"user_interrupted"}),
        json!({"kind":"input_admitted","at_us":AT+3,"turn":10,"owner":{"generation":12}}),
        // Normal completion is not a cancellation and must not become one.
        json!({"kind":"turn_finished","at_us":AT+4,"turn":10,"generation":12,"outcome":"audio_written_unscored","playback_gaps":0}),
    ];
    assert_eq!(sink.scan(&trace, AT + 5), None);
    let mut bytes = [0; CUE_MAX_BYTES];
    for (index, kind) in [
        "input_admitted",
        "local_endpoint",
        "cancelled",
        "input_admitted",
        "turn_completed",
    ]
    .iter()
    .enumerate()
    {
        let count = peer.recv(&mut bytes).unwrap();
        let cue: Value = serde_json::from_slice(&bytes[..count]).unwrap();
        assert_eq!(cue["sequence"], index + 1);
        assert_eq!(cue["kind"], *kind);
        assert_eq!(cue["event_us"], AT + index as u64);
        assert_eq!(cue["turn"], if index >= 3 { 10 } else { 9 });
        assert_eq!(cue["generation"], if index >= 3 { 12 } else { 11 });
        assert_eq!(
            cue["reason"],
            if index == 2 {
                json!("user_interrupted")
            } else {
                Value::Null
            }
        );
        if index == 4 {
            assert_eq!(cue["outcome"], "audio_written_unscored");
            assert_eq!(cue["playback_gaps"], 0);
        }
    }
    assert_eq!(
        peer.recv(&mut bytes).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
}

#[test]
fn cue_event_to_peer_receipt_latency_is_reported_with_a_bounded_send_path() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    let mut trace = Vec::new();
    let mut elapsed = Vec::new();
    let mut bytes = [0; CUE_MAX_BYTES];
    for turn in 1..=200_u64 {
        let at = lamp_ipc::monotonic_us();
        trace.push(
            json!({"kind":"input_admitted","at_us":at,"turn":turn,"owner":{"generation":turn+1}}),
        );
        assert_eq!(sink.scan(&trace, lamp_ipc::monotonic_us()), None);
        let count = peer.recv(&mut bytes).unwrap();
        let received = lamp_ipc::monotonic_us();
        let cue: Value = serde_json::from_slice(&bytes[..count]).unwrap();
        assert_eq!(cue["sequence"], turn);
        assert!(received < cue["expires_us"].as_u64().unwrap());
        elapsed.push(received - at);
    }
    let first = elapsed[0];
    elapsed.sort_unstable();
    let p50 = elapsed[99];
    let p95 = elapsed[189];
    eprintln!(
        "cue_local_socket_only n=200 first_us={first} p50_us={p50} p95_us={p95} max_us={}; excludes coordinator scheduling, relay, acoustic and device paths",
        elapsed[199]
    );
    assert!(
        p95 < 2_000,
        "local cue observation missed the 2 ms p95 host target: {p95}"
    );
}

#[test]
fn stale_cue_and_peer_loss_latch_invalid_without_blocking_or_retrying() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    let trace = vec![json!({"kind":"listening_ready","at_us":AT})];
    assert!(sink.scan(&trace, AT + 100_000).is_some());
    assert!(sink.fault().is_some());
    assert_eq!(
        sink.scan(
            &[
                trace[0].clone(),
                json!({"kind":"run_end","at_us":AT+100_001})
            ],
            AT + 100_001
        ),
        None
    );
    assert_eq!(
        peer.recv(&mut [0; CUE_MAX_BYTES]).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
    let private2 = Private::new();
    let (peer, mut sink) = bound_cue(&private2);
    drop(peer);
    assert!(sink.scan(&trace, AT).is_some());
}

#[test]
fn cue_backpressure_is_bounded_and_latches_without_affecting_authority() {
    let private = Private::new();
    let (_peer, mut sink) = bound_cue(&private);
    let trace:Vec<_>=(0..4000).map(|i|json!({"kind":"speaker_first_write","at_us":AT+i,"turn":1,"owner":{"generation":3},"playback_sequence":i+1})).collect();
    let started = Instant::now();
    assert!(sink.scan(&trace, AT + 4000).is_some());
    assert!(started.elapsed() < Duration::from_secs(1));
    assert!(sink.fault().is_some());
}

#[test]
fn cue_endpoint_must_be_private_same_user_socket_not_symlink_or_file() {
    let private = Private::new();
    let path = private.path.join("cue");
    fs::write(&path, []).unwrap();
    assert!(CueSink::connect(&path).is_err());
    fs::remove_file(&path).unwrap();
    let _peer = UnixDatagram::bind(&path).unwrap();
    let link = private.path.join("link");
    symlink(&path, &link).unwrap();
    assert!(CueSink::connect(&link).is_err());
    assert!(CueSink::connect(Path::new("relative")).is_err());
    fs::set_permissions(&private.path, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(CueSink::connect(&path).is_err());
}

#[test]
fn real_fixture_worker_exchanges_exact_pcm_and_stops_without_devices_or_cloud() {
    let private = Private::new();
    let (wav, hash, expected) = private.wave(24_000, 1, 2500);
    let dir = SessionDirectory::create().unwrap();
    let mut worker = Worker::spawn(
        Path::new(env!("CARGO_BIN_EXE_lamp-live")),
        &dir.path,
        "provider",
        boot(),
        &["--fixture", wav.to_str().unwrap(), &hash],
    )
    .unwrap();
    let now = lamp_ipc::monotonic_us();
    let mut controller = Controller::new(boot(), time(now));
    controller
        .set_microphone_permission(time(now), Permission::Allowed)
        .unwrap();
    controller
        .set_capture(time(now), CaptureState::RetainingUntil(time(now + 900_000)))
        .unwrap();
    controller
        .set_admission(time(now), AdmissionState::OpenUntil(time(now + 900_000)))
        .unwrap();
    let turn = controller.admit(time(now), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(time(now), turn).unwrap();
    let state = controller.snapshot(time(lamp_ipc::monotonic_us())).unwrap();
    worker
        .channels
        .control
        .send(Control::Authority { snapshot: state })
        .unwrap();
    for command in [
        ProviderInput::Start {
            request: 1,
            privacy_generation: state.microphone_generation(),
        },
        ProviderInput::Audio {
            request: 1,
            privacy_generation: state.microphone_generation(),
            sequence: 1,
            read_completed_at_us: now,
            samples: vec![1; 160],
        },
        ProviderInput::End {
            request: 1,
            privacy_generation: state.microphone_generation(),
        },
    ] {
        worker.channels.data.send(command).unwrap();
    }
    let deadline = Instant::now() + Duration::from_millis(200);
    let mut pcm = Vec::new();
    let mut received = 0;
    loop {
        let output = worker.channels.data.receive::<ProviderOutput>().unwrap();
        if output.is_some() {
            received += 1;
            // This short fixture has space for every sample. Return two
            // boot-scoped packet reservations, counting metadata as well.
            worker
                .channels
                .control
                .send(Control::ProviderOutputCapacity {
                    through: received + 2,
                })
                .unwrap();
        }
        match output {
            Some(ProviderOutput::Audio {
                request, samples, ..
            }) => {
                assert_eq!(request, 1);
                pcm.extend(samples);
            }
            Some(ProviderOutput::TurnComplete {
                request: 1,
                idle: true,
            }) => break,
            _ => {}
        }
        assert!(Instant::now() < deadline, "fixture worker failed to finish");
        std::thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(pcm, expected);
    worker.shutdown().unwrap();
}

#[test]
fn delayed_fresh_authority_cannot_revive_an_expired_original_lease() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.start();
    rig.audio();
    rig.end();
    let late = rig.now + 250_000;
    rig.owner
        .set_capture(
            time(late),
            CaptureState::RetainingUntil(time(late + 500_000)),
        )
        .unwrap();
    rig.owner
        .set_admission(time(late), AdmissionState::OpenUntil(time(late + 500_000)))
        .unwrap();
    let state = rig.owner.snapshot(time(late)).unwrap();
    assert!(rig.engine.install(late, state).is_err());
    assert!(
        rig.engine
            .send_one(late, |_| panic!("expired provider revived"))
            .is_err()
    );
}

#[test]
fn maximal_cue_fields_fit_the_fixed_datagram_without_truncation() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    sink.set_boot(BootId::new([255; 16]).unwrap());
    let event_us = u64::MAX - 200_000;
    let trace = vec![
        json!({"kind":"capture_started","details":{"epoch":u64::MAX}}),
        json!({"kind":"reference_clock","details":{"playback_epoch":u64::MAX}}),
        json!({"kind":"turn_finished","at_us":event_us,"turn":u64::MAX,"owner":{"generation":u64::MAX},"outcome":"input_discontinuity"}),
        json!({"kind":"speaker_first_write","at_us":event_us,"turn":u64::MAX,"owner":{"generation":u64::MAX},"playback_sequence":u64::MAX}),
        json!({"kind":"speech_final_sample_retired","at_us":event_us,"turn":u64::MAX,"owner":{"generation":u64::MAX},"playback_sequence":u64::MAX}),
        json!({"kind":"turn_finished","at_us":event_us,"turn":u64::MAX,"generation":u64::MAX,"outcome":"audio_written_with_playback_gaps_unscored","playback_gaps":u64::MAX}),
    ];
    assert_eq!(sink.scan(&trace, event_us + 1), None);
    let mut data = [0; CUE_MAX_BYTES];
    let count = peer.recv(&mut data).unwrap();
    let parsed: Value = serde_json::from_slice(&data[..count]).unwrap();
    assert_eq!(parsed["turn"], u64::MAX);
    assert_eq!(parsed["expires_us"], event_us + 100_000);
    assert_eq!(parsed["reason"], "input_discontinuity");
    for kind in ["speaker_first_write", "speech_retired", "turn_completed"] {
        let count = peer.recv(&mut data).unwrap();
        let parsed: Value = serde_json::from_slice(&data[..count]).unwrap();
        assert_eq!(parsed["kind"], kind);
        assert_eq!(parsed["turn"], u64::MAX);
        assert_eq!(parsed["generation"], u64::MAX);
        assert_eq!(parsed["expires_us"], event_us + 100_000);
        if kind == "turn_completed" {
            assert_eq!(parsed["playback_gaps"], u64::MAX);
            assert_eq!(
                parsed["outcome"],
                "audio_written_with_playback_gaps_unscored"
            );
        } else {
            assert_eq!(parsed["playback_sequence"], u64::MAX);
        }
        // The internal datagram sequence here has one digit. Reserve another
        // 19 bytes for its eventual maximum u64 representation too.
        assert!(count + 19 <= CUE_MAX_BYTES);
        eprintln!(
            "maximal_{kind}_cue_bytes_including_max_sequence={}",
            count + 19
        );
    }
}

#[test]
fn invalid_occurrence_or_completion_metadata_latches_observation_failure() {
    for event in [
        json!({"kind":"speaker_first_write","at_us":AT,"turn":1,"owner":{"generation":2}}),
        json!({"kind":"speech_final_sample_retired","at_us":AT,"turn":1,"owner":{"generation":2},"playback_sequence":0}),
        json!({"kind":"turn_finished","at_us":AT,"turn":1,"outcome":"audio_written_unscored","playback_gaps":0}),
        json!({"kind":"turn_finished","at_us":AT,"turn":1,"generation":2,"outcome":"audio_written_unscored"}),
    ] {
        let private = Private::new();
        let (peer, mut sink) = bound_cue(&private);
        assert!(sink.scan(&[event], AT + 1).is_some());
        assert!(sink.fault().is_some());
        assert_eq!(
            peer.recv(&mut [0; CUE_MAX_BYTES]).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn multiple_playback_occurrences_remain_one_turn_until_completion() {
    let private = Private::new();
    let (peer, mut sink) = bound_cue(&private);
    let trace = vec![
        json!({"kind":"input_admitted","at_us":AT,"turn":7,"owner":{"generation":9}}),
        json!({"kind":"local_endpoint","at_us":AT+1,"turn":7,"owner":{"generation":9}}),
        json!({"kind":"speaker_first_write","at_us":AT+2,"turn":7,"owner":{"generation":9},"playback_sequence":11}),
        json!({"kind":"speech_final_sample_retired","at_us":AT+3,"turn":7,"owner":{"generation":9},"playback_sequence":11}),
        json!({"kind":"speaker_first_write","at_us":AT+4,"turn":7,"owner":{"generation":9},"playback_sequence":12}),
        json!({"kind":"speech_final_sample_retired","at_us":AT+5,"turn":7,"owner":{"generation":9},"playback_sequence":12}),
        json!({"kind":"turn_finished","at_us":AT+6,"turn":7,"generation":9,"outcome":"audio_written_with_playback_gaps_unscored","playback_gaps":2}),
    ];
    assert_eq!(sink.scan(&trace, AT + 7), None);
    for (index, expected) in [None, None, Some(11), Some(11), Some(12), Some(12), None]
        .into_iter()
        .enumerate()
    {
        let mut bytes = [0; CUE_MAX_BYTES];
        let count = peer.recv(&mut bytes).unwrap();
        let cue: Value = serde_json::from_slice(&bytes[..count]).unwrap();
        assert_eq!(cue["sequence"], index + 1);
        assert_eq!(cue["turn"], 7);
        assert_eq!(cue["generation"], 9);
        assert_eq!(cue["playback_sequence"].as_u64(), expected);
        if index == 6 {
            assert_eq!(cue["kind"], "turn_completed");
            assert_eq!(cue["playback_gaps"], 2);
        } else {
            assert_ne!(cue["kind"], "turn_completed");
        }
    }
}

fn channel_pair(private: &Private) -> (WorkerChannels, WorkerChannels) {
    let worker_boot = BootId::new([8; 16]).unwrap();
    let mut parent =
        WorkerChannels::bind(&private.path, "provider", boot(), worker_boot, false).unwrap();
    let mut worker =
        WorkerChannels::bind(&private.path, "provider", boot(), worker_boot, true).unwrap();
    parent.connect(&private.path, "provider", false).unwrap();
    worker.connect(&private.path, "provider", true).unwrap();
    (parent, worker)
}

#[test]
fn separate_sockets_new_authority_in_empty_drain_gap_precedes_input_submission() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    assert_eq!(rig.drain()[0]["kind"], "ready");
    let (mut parent, mut worker) = channel_pair(&private);
    let at = AT + 100;
    let new_owner = rig
        .owner
        .admit(time(at), AdmittedInput::Interruption)
        .unwrap();
    let state = rig.owner.snapshot(time(at)).unwrap();
    let mut injected = false;
    assert!(
        rig.engine
            .service_step(
                || at,
                || {
                    let control = worker.control.receive()?;
                    if !injected {
                        assert!(control.is_none());
                        injected = true;
                        // Force the reported interleaving, without threads or wall-clock waits:
                        // worker saw control empty, then parent sends authority before data.
                        parent
                            .control
                            .send(Control::Authority { snapshot: state })?;
                        parent.data.send(ProviderInput::Start {
                            request: new_owner.turn(),
                            privacy_generation: rig.privacy,
                        })?;
                    }
                    Ok(control)
                },
                &mut worker.data
            )
            .unwrap()
    );
    assert!(injected);
    assert!(matches!(
        parent.data.receive::<ProviderOutput>().unwrap(),
        Some(ProviderOutput::Started {
            request: 2,
            waiting_for_barrier: false
        })
    ));
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
}

#[test]
fn separate_sockets_stop_or_privacy_in_empty_drain_gap_prevents_input_and_reply() {
    for privacy_close in [false, true] {
        let private = Private::new();
        let mut rig = Rig::new(&private);
        rig.start();
        rig.audio();
        rig.end();
        // Leave actual reply PCM due, after consuming only Ready and Started.
        for _ in 0..2 {
            rig.now += 1;
            assert!(rig.engine.send_one(rig.now, |_| Ok(())).unwrap());
        }
        let (mut parent, mut worker) = channel_pair(&private);
        let at = rig.now + 100;
        let priority = if privacy_close {
            rig.owner
                .set_microphone_permission(time(at), Permission::Denied)
                .unwrap();
            Control::Authority {
                snapshot: rig.owner.snapshot(time(at)).unwrap(),
            }
        } else {
            Control::Stop
        };
        let mut priority = Some(priority);
        assert!(
            !rig.engine
                .service_step(
                    || at,
                    || {
                        let control = worker.control.receive()?;
                        if let Some(priority) = priority.take() {
                            assert!(control.is_none());
                            parent.control.send(priority)?;
                            parent.data.send(ProviderInput::Start {
                                request: 1,
                                privacy_generation: rig.privacy,
                            })?;
                        }
                        Ok(control)
                    },
                    &mut worker.data
                )
                .unwrap()
        );
        assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
        assert!(
            rig.engine
                .send_one(at, |_| panic!("revoked reply escaped"))
                .is_err()
        );
    }
}

#[test]
fn full_second_control_slice_retains_only_original_input_and_suppresses_output() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    rig.drain();
    let (mut parent, mut worker) = channel_pair(&private);
    let at = AT + 100;
    rig.owner
        .admit(time(at), AdmittedInput::Interruption)
        .unwrap();
    let mut states: std::collections::VecDeque<_> = (0..17)
        .map(|_| rig.owner.snapshot(time(at)).unwrap())
        .collect();
    let mut injected = false;
    assert!(
        rig.engine
            .service_step(
                || at,
                || {
                    let control = worker.control.receive()?;
                    if !injected {
                        assert!(control.is_none());
                        injected = true;
                        parent.control.send(Control::Authority {
                            snapshot: states.pop_front().unwrap(),
                        })?;
                        parent.data.send(ProviderInput::Start {
                            request: 2,
                            privacy_generation: rig.privacy,
                        })?;
                        parent.data.send(ProviderInput::Audio {
                            request: 2,
                            privacy_generation: rig.privacy,
                            sequence: 1,
                            read_completed_at_us: at,
                            samples: vec![1; 160],
                        })?;
                    } else if control.is_some()
                        && let Some(snapshot) = states.pop_front()
                    {
                        // Real sockets, but one queued control at a time: independent of
                        // the platform's datagram queue-length limit.
                        parent.control.send(Control::Authority { snapshot })?;
                    }
                    Ok(control)
                },
                &mut worker.data
            )
            .unwrap()
    );
    assert!(states.is_empty()); // Seventeenth authority remains queued.
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
    assert!(
        rig.engine
            .service_step(|| at + 1, || worker.control.receive(), &mut worker.data)
            .unwrap()
    );
    assert!(matches!(
        parent.data.receive::<ProviderOutput>().unwrap(),
        Some(ProviderOutput::Started { request: 2, .. })
    ));
    // This tick submitted the original Start, never consumed the next Audio.
    assert!(matches!(
        worker.data.receive::<ProviderInput>().unwrap(),
        Some(ProviderInput::Audio {
            request: 2,
            sequence: 1,
            ..
        })
    ));
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
}

#[test]
fn deferred_input_keeps_original_age_and_cannot_wait_indefinitely_on_control() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    let (mut parent, mut worker) = channel_pair(&private);
    let at = AT + 100;
    let mut states: std::collections::VecDeque<_> = (0..16)
        .map(|_| rig.owner.snapshot(time(at)).unwrap())
        .collect();
    let mut injected = false;
    assert!(
        rig.engine
            .service_step(
                || at,
                || {
                    let control = worker.control.receive()?;
                    if !injected {
                        assert!(control.is_none());
                        injected = true;
                        parent.control.send(Control::Authority {
                            snapshot: states.pop_front().unwrap(),
                        })?;
                        parent.data.send(ProviderInput::Start {
                            request: 1,
                            privacy_generation: rig.privacy,
                        })?;
                    } else if control.is_some()
                        && let Some(snapshot) = states.pop_front()
                    {
                        parent.control.send(Control::Authority { snapshot })?;
                    }
                    Ok(control)
                },
                &mut worker.data
            )
            .unwrap()
    );
    assert!(states.is_empty());
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
    let error = rig
        .engine
        .service_step(
            || at + 100_000,
            || panic!("expired retained input must fail before another deferral"),
            &mut worker.data,
        )
        .unwrap_err();
    assert!(error.to_string().contains("retained input expired"));
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
    assert!(
        rig.engine
            .send_one(at + 100_000, |_| panic!("expired input revived"))
            .is_err()
    );
}

#[test]
fn empty_second_drain_does_not_retry_an_input_without_matching_authority() {
    let private = Private::new();
    let mut rig = Rig::new(&private);
    let (mut parent, mut worker) = channel_pair(&private);
    parent
        .data
        .send(ProviderInput::Start {
            request: 2,
            privacy_generation: rig.privacy,
        })
        .unwrap();
    assert!(
        rig.engine
            .service_step(|| AT + 1, || worker.control.receive(), &mut worker.data)
            .is_err()
    );
    assert!(parent.data.receive::<ProviderOutput>().unwrap().is_none());
}
