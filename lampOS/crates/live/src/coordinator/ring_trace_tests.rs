use super::*;
use crate::ring_wire::RingFrame;

struct Fixture {
    _directory: SessionDirectory,
    worker: Worker,
    peer: WorkerChannels,
    owner: Controller,
    policy: RingChoreographer,
    trace: Vec<Value>,
    ring_trace: RingTrace,
    requests: u64,
    receipts: u64,
}

impl Fixture {
    fn new() -> Self {
        let directory = SessionDirectory::create().unwrap();
        let (worker, peer) = Worker::sleeping_fixture(&directory.path, "ring");
        let mut owner = Controller::new(new_boot().unwrap(), at(1));
        owner
            .set_microphone_permission(at(1), Permission::Allowed)
            .unwrap();
        renew_input(&mut owner, 1);
        owner.admit(at(1), AdmittedInput::NewTurn).unwrap();
        Self {
            _directory: directory,
            worker,
            peer,
            owner,
            policy: RingChoreographer::new(24).unwrap(),
            trace: Vec::new(),
            ring_trace: RingTrace::default(),
            requests: 0,
            receipts: 0,
        }
    }

    fn tick(&mut self, us: u64) -> Result<()> {
        service_ring_with_clock(
            Some(&mut self.worker),
            Some(&mut self.policy),
            &mut self.owner,
            &mut self.trace,
            &mut self.ring_trace,
            || at(us),
        )
    }

    fn exchange(&mut self, us: u64) -> Result<RingFrame> {
        renew_input(&mut self.owner, us);
        self.tick(us)?;
        let frame = self
            .peer
            .data
            .receive::<RingFrame>()?
            .expect("one actual frame per renewal");
        assert!(matches!(self.peer.control.receive::<Control>()?,
            Some(Control::Authority { snapshot }) if snapshot == frame.snapshot));
        assert!(self.peer.control.receive::<Control>()?.is_none());
        assert!(self.peer.data.receive::<RingFrame>()?.is_none());
        self.requests += 1;
        self.peer.control.send(WorkerEvent::Ring {
            report: RingFeedback::Presented {
                owner: frame.permit.owner(),
                phase: frame.phase,
                requested_at_us: frame.requested_at_us,
                write_started_at_us: us + 100,
                write_finished_at_us: us + 200,
            },
        })?;
        self.tick(us + 200)?;
        self.receipts += 1;
        assert!(self.policy.pending().is_none());
        Ok(frame)
    }
}

fn at(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}
fn renew_input(owner: &mut Controller, us: u64) {
    owner
        .set_capture(
            at(us),
            CaptureState::RetainingUntil(at(us + INPUT_LEASE_US)),
        )
        .unwrap();
    owner
        .set_admission(at(us), AdmissionState::OpenUntil(at(us + INPUT_LEASE_US)))
        .unwrap();
}

#[test]
fn ring_trace_supports_more_than_ten_thousand_lease_pairs() {
    let mut f = Fixture::new();
    for index in 0..10_050 {
        f.exchange(1 + index * crate::choreography::RENEW_INTERVAL_US)
            .unwrap_or_else(|error| {
                panic!(
                    "renewal {index}, {} retained events: {error}",
                    f.trace.len()
                )
            });
    }
    assert_eq!((f.requests, f.receipts), (10_050, 10_050));
    assert!(f.trace.len() < MAX_TRACE_EVENTS);
    eprintln!(
        "10050 actual socket request/receipt pairs, {} retained events; virtual clock, no hardware",
        f.trace.len()
    );
}

impl Fixture {
    fn request(&mut self, us: u64) -> RingFrame {
        renew_input(&mut self.owner, us);
        self.tick(us).unwrap();
        let frame = self.peer.data.receive::<RingFrame>().unwrap().unwrap();
        assert!(matches!(self.peer.control.receive::<Control>().unwrap(),
            Some(Control::Authority { snapshot }) if snapshot == frame.snapshot));
        self.requests += 1;
        frame
    }

    fn submit(&mut self, report: RingFeedback, observed_us: u64) -> Result<()> {
        self.peer.control.send(WorkerEvent::Ring { report })?;
        self.tick(observed_us)
    }

    fn admitted(&mut self, us: u64) -> TurnOwner {
        self.owner.snapshot(at(us)).unwrap().owner().unwrap()
    }

    fn start_playback(&mut self, us: u64) -> PlaybackToken {
        let owner = self.admitted(us);
        let plan = self
            .owner
            .plan_output(at(us), owner, OutputKind::Speech, None)
            .unwrap();
        let permit = self
            .owner
            .issue_output(at(us), plan, INPUT_LEASE_US)
            .unwrap();
        self.owner.playback_started(at(us), permit).unwrap()
    }
}

fn presented(frame: RingFrame, start: u64, finish: u64) -> RingFeedback {
    RingFeedback::Presented {
        owner: frame.permit.owner(),
        phase: frame.phase,
        requested_at_us: frame.requested_at_us,
        write_started_at_us: start,
        write_finished_at_us: finish,
    }
}

fn count(trace: &[Value], kind: &str) -> usize {
    trace.iter().filter(|event| event["kind"] == kind).count()
}

fn head_requests(trace: &[Value]) -> usize {
    trace
        .iter()
        .filter(|event| {
            event["kind"] == "ring_requested" && event["trace_role"] == "cue_or_periodic_head"
        })
        .count()
}

fn sum(trace: &[Value], field: &str) -> u64 {
    trace
        .iter()
        .filter(|event| event["kind"] == "ring_renewals")
        .map(|event| event[field].as_u64().unwrap())
        .sum()
}

fn last_summary(trace: &[Value]) -> &Value {
    trace
        .iter()
        .rev()
        .find(|event| event["kind"] == "ring_renewals")
        .unwrap()
}

#[test]
fn ring_trace_full_600_seconds_keeps_voice_transitions_and_fault_within_cap() {
    let mut f = Fixture::new();
    let mut playback = None;
    f.trace
        .resize_with(256, || json!({"kind":"fixture_startup_shutdown_reserve"}));
    for index in 0..30_000_u64 {
        let us = 1 + index * crate::choreography::RENEW_INTERVAL_US;
        renew_input(&mut f.owner, us);
        let previous = f.admitted(us);
        match index % 1_500 {
            0 if index != 0 => {
                f.owner.cancel_turn(at(us), previous).unwrap();
                record(&mut f.trace, json!({"kind":"turn_cancelled", "at_us":us, "turn":previous.turn(), "owner":previous})).unwrap();
                let next = f.owner.admit(at(us), AdmittedInput::Interruption).unwrap();
                record(
                    &mut f.trace,
                    json!({"kind":"input_admitted", "at_us":us, "turn":next.turn(), "owner":next}),
                )
                .unwrap();
                playback = None;
            }
            25 => {
                f.owner.input_ended(at(us), previous).unwrap();
                record(
                    &mut f.trace,
                    json!({"kind":"local_endpoint", "at_us":us, "turn":previous.turn()}),
                )
                .unwrap();
            }
            50 | 1_425 => {
                playback = Some(f.start_playback(us));
                record(&mut f.trace, json!({"kind":"speaker_first_write", "at_us":us, "turn":previous.turn(), "playback_sequence":playback.unwrap().sequence()})).unwrap();
            }
            1_400 => {
                let token = playback.take().unwrap();
                f.owner.playback_ended(at(us), token).unwrap();
                record(&mut f.trace, json!({"kind":"speech_final_sample_retired", "at_us":us, "turn":previous.turn(), "playback_sequence":token.sequence()})).unwrap();
            }
            _ => {}
        }
        // A deliberately dense bounded voice trace: 25 PCM batches/s, two
        // transcript updates/s and one reference timing receipt/s for all 600s,
        // even while the mock is listening. These use the production cap.
        if index % 2 == 0 {
            record(&mut f.trace, json!({"kind":"provider_audio_enqueued", "at_us":us, "turn":previous.turn(), "frames":960, "sequence":index/2+1})).unwrap();
        }
        if index % 25 == 0 {
            record(
                &mut f.trace,
                json!({"kind":"transcript", "at_us":us, "turn":previous.turn(), "text":"fixture"}),
            )
            .unwrap();
        }
        if index % 50 == 0 {
            record(
                &mut f.trace,
                json!({"kind":"echo_reference_clock", "at_us":us}),
            )
            .unwrap();
        }
        f.exchange(us)
            .unwrap_or_else(|e| panic!("600s fixture at {us}: {e}"));
        let expected_phase = match index % 1_500 {
            0 => Some("listening"),
            25 | 1_400 => Some("waiting"),
            50 | 1_425 => Some("speaking"),
            _ => None,
        };
        if let Some(phase) = expected_phase {
            let current_owner = f.admitted(us + 200);
            let request = f
                .trace
                .iter()
                .rev()
                .find(|event| event["kind"] == "ring_requested")
                .unwrap();
            assert_eq!(request["at_us"], us, "phase transition was coalesced");
            assert_eq!(request["phase"], phase);
            assert_eq!(
                request["owner"],
                serde_json::to_value(current_owner).unwrap()
            );
        }
    }
    assert_eq!((f.requests, f.receipts), (30_000, 30_000));
    assert_eq!(
        head_requests(&f.trace) as u64 + sum(&f.trace, "requested"),
        30_000
    );
    let raw_presentations = f
        .trace
        .iter()
        .filter(|event| event["kind"] == "ring_feedback" && event["details"]["kind"] == "presented")
        .count() as u64;
    assert_eq!(
        raw_presentations + sum(&f.trace, "validated_presentations"),
        30_000
    );
    assert_eq!(sum(&f.trace, "unacknowledged_requests"), 0);
    assert_eq!(count(&f.trace, "turn_cancelled"), 19);
    assert_eq!(count(&f.trace, "provider_audio_enqueued"), 15_000);
    assert_eq!(count(&f.trace, "transcript"), 1_200);
    // A real worker fault must remain fatal and visible after all renewals.
    f.peer
        .control
        .send(WorkerEvent::Fault {
            code: "injected_after_600s".into(),
        })
        .unwrap();
    let error = f.tick(600_000_001).unwrap_err();
    assert!(error.to_string().contains("injected_after_600s"));
    assert_eq!(count(&f.trace, "ring_service_fault"), 1);
    assert!(f.trace.len() < MAX_TRACE_EVENTS - 200);
    eprintln!(
        "600 virtual seconds: 30000 actual socket frame/receipt pairs; {} retained events including 16800 dense voice events, 256 startup/shutdown reserve, cancellations and injected terminal fault",
        f.trace.len()
    );
}

#[test]
fn ring_trace_renewal_without_receipt_never_claims_success_and_times_out() {
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    let frame = f.request(20_001);
    let summary = last_summary(&f.trace);
    assert_eq!(summary["requested"], 1);
    assert_eq!(summary["validated_presentations"], 0);
    assert_eq!(summary["unacknowledged_requests"], 1);
    assert!(summary["last_presented"].is_null());
    assert_eq!(
        summary["last_request"],
        serde_json::to_value(frame).unwrap()
    );
    let error = f.tick(120_001).unwrap_err();
    assert!(error.to_string().contains("100 ms"));
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 1);
    assert_eq!(
        f.trace.last().unwrap()["pending_frame"],
        serde_json::to_value(frame).unwrap()
    );
}

#[test]
fn ring_trace_invalid_renewal_receipts_remain_individual_and_do_not_increment_success() {
    for case in ["identity", "future", "slow"] {
        let mut f = Fixture::new();
        f.exchange(1).unwrap();
        let frame = f.request(20_001);
        let mut report = presented(frame, 20_101, 20_201);
        let observed = match case {
            "identity" => {
                if let RingFeedback::Presented {
                    requested_at_us, ..
                } = &mut report
                {
                    *requested_at_us += 1;
                }
                20_202
            }
            "future" => 20_200,
            "slow" => {
                report = presented(frame, 20_101, 70_002);
                70_003
            }
            _ => unreachable!(),
        };
        assert!(f.submit(report, observed).is_err(), "{case}");
        assert_eq!(last_summary(&f.trace)["validated_presentations"], 0);
        assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 1);
        let feedback = f
            .trace
            .iter()
            .rev()
            .find(|event| event["kind"] == "ring_feedback")
            .unwrap();
        assert_eq!(feedback["details"], serde_json::to_value(report).unwrap());
        assert_eq!(
            feedback["requested_frame"],
            serde_json::to_value(frame).unwrap()
        );
        assert!(feedback["validation_error"].is_string());
        assert_eq!(count(&f.trace, "ring_service_fault"), 1);
    }
}

#[test]
fn ring_trace_superseded_renewal_keeps_raw_rejection_and_new_owner_transition() {
    use crate::ring_wire::RingRejection;
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    let previous = f.request(20_001);
    let next_owner = f
        .owner
        .admit(at(20_100), AdmittedInput::Interruption)
        .unwrap();
    let report = RingFeedback::Rejected {
        owner: previous.permit.owner(),
        phase: previous.phase,
        requested_at_us: previous.requested_at_us,
        reason: RingRejection::StaleOwner,
    };
    f.submit(report, 20_200).unwrap();
    let next = f.peer.data.receive::<RingFrame>().unwrap().unwrap();
    assert_eq!(next.permit.owner(), next_owner);
    assert_eq!(next.phase, previous.phase);
    assert!(
        matches!(f.peer.control.receive::<Control>().unwrap(), Some(Control::Authority { snapshot }) if snapshot == next.snapshot)
    );
    assert_eq!(last_summary(&f.trace)["validated_rejections"], 1);
    assert_eq!(last_summary(&f.trace)["validated_presentations"], 0);
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 0);
    assert_eq!(head_requests(&f.trace), 2);
    let rejected = f
        .trace
        .iter()
        .find(|event| event["details"]["kind"] == "rejected")
        .unwrap();
    assert_eq!(rejected["details"], serde_json::to_value(report).unwrap());
    assert_eq!(
        rejected["requested_frame"],
        serde_json::to_value(previous).unwrap()
    );
}

#[test]
fn ring_trace_blank_does_not_acknowledge_pending_and_next_request_is_raw() {
    use crate::ring_wire::RingRejection;
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    let frame = f.request(20_001);
    let blank = RingFeedback::Blanked {
        reason: RingBlankReason::AuthorityLost {
            reason: RingRejection::StalePresentation,
        },
        write_started_at_us: 20_011,
        write_finished_at_us: 20_021,
    };
    f.submit(blank, 20_030).unwrap();
    assert_eq!(f.policy.pending(), Some(frame));
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 1);
    f.submit(presented(frame, 20_101, 20_201), 20_202).unwrap();
    f.exchange(40_001).unwrap();
    assert_eq!(head_requests(&f.trace), 2);
    assert_eq!(
        f.trace
            .iter()
            .filter(|event| event["details"]["kind"] == "blanked")
            .count(),
        1
    );
}

#[test]
fn ring_trace_same_turn_new_playback_occurrence_keeps_raw_request() {
    let mut f = Fixture::new();
    let owner = f.admitted(2);
    f.owner.input_ended(at(2), owner).unwrap();
    let first = f.start_playback(3);
    f.exchange(4).unwrap();
    f.owner.playback_ended(at(10_000), first).unwrap();
    let next = f.start_playback(10_001);
    assert_ne!(first.sequence(), next.sequence());
    f.exchange(20_004).unwrap();
    assert_eq!(head_requests(&f.trace), 2);
    assert_eq!(count(&f.trace, "ring_renewals"), 0);
    let sequences: Vec<_> = f
        .trace
        .iter()
        .filter(|event| event["kind"] == "ring_requested")
        .map(|event| event["playback_sequence"].as_u64().unwrap())
        .collect();
    assert_eq!(sequences, [first.sequence(), next.sequence()]);
}

#[test]
fn ring_trace_retains_original_first_last_receipts_and_latency_extrema() {
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    let first = f.request(20_001);
    let first_report = presented(first, 20_101, 20_701);
    f.submit(first_report, 20_901).unwrap();
    let last = f.request(40_001);
    let last_report = presented(last, 40_051, 40_101);
    f.submit(last_report, 40_501).unwrap();
    let summary = last_summary(&f.trace);
    assert_eq!(summary["at_us"], first.requested_at_us);
    assert_eq!(
        summary["first_request"],
        serde_json::to_value(first).unwrap()
    );
    assert_eq!(summary["last_request"], serde_json::to_value(last).unwrap());
    assert_eq!(
        summary["first_presented"],
        serde_json::to_value(first_report).unwrap()
    );
    assert_eq!(
        summary["last_presented"],
        serde_json::to_value(last_report).unwrap()
    );
    assert_eq!(summary["last_updated_at_us"], 40_501);
    assert_eq!(summary["request_to_write_min_us"], 100);
    assert_eq!(summary["request_to_write_max_us"], 700);
    assert_eq!(summary["request_to_write_total_us"], 800);
    assert_eq!(summary["validated_presentations"], 2);
}

#[test]
fn ring_trace_audio_only_service_produces_no_ring_events() {
    let mut owner = Controller::new(new_boot().unwrap(), at(1));
    let mut trace = vec![json!({"kind":"voice_fixture"})];
    let before = trace.clone();
    let mut ring_trace = RingTrace::default();
    service_ring_with_clock(None, None, &mut owner, &mut trace, &mut ring_trace, || {
        at(2)
    })
    .unwrap();
    assert_eq!(trace, before);
}

#[test]
fn ring_trace_duplicate_presentation_keeps_the_actual_success_count() {
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    let frame = f.exchange(20_001).unwrap();
    let duplicate = presented(frame, 20_101, 20_201);
    assert!(f.submit(duplicate, 20_202).is_err());
    assert_eq!(last_summary(&f.trace)["validated_presentations"], 1);
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 0);
    let rejected_receipt = f
        .trace
        .iter()
        .rev()
        .find(|event| event["kind"] == "ring_feedback")
        .unwrap();
    assert_eq!(
        rejected_receipt["details"],
        serde_json::to_value(duplicate).unwrap()
    );
    assert!(rejected_receipt["validation_error"].is_string());
}

#[test]
fn ring_trace_preserves_the_existing_global_cap() {
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    f.trace
        .resize_with(MAX_TRACE_EVENTS - 1, || json!({"kind":"other_evidence"}));
    renew_input(&mut f.owner, 20_001);
    let error = f.tick(20_001).unwrap_err();
    assert!(error.to_string().contains("trace capacity reached"));
    assert_eq!(f.trace.len(), MAX_TRACE_EVENTS - 1);
}

#[test]
fn ring_trace_same_waiting_phase_after_unobserved_playback_cycle_is_still_a_transition() {
    let mut f = Fixture::new();
    let owner = f.admitted(2);
    f.owner.input_ended(at(2), owner).unwrap();
    f.exchange(3).unwrap();
    let token = f.start_playback(1_000);
    f.owner.playback_ended(at(1_001), token).unwrap();
    // No intermediate ring request was sent. The new waiting request has the
    // same owner/enum and no playback token, but a new presentation lineage.
    f.exchange(1_002).unwrap();
    assert_eq!(head_requests(&f.trace), 2);
    assert_eq!(count(&f.trace, "ring_renewals"), 0);
}

#[test]
fn ring_trace_retains_raw_tail_for_later_backdated_retirement_evidence() {
    let mut f = Fixture::new();
    let owner = f.admitted(2);
    f.owner.input_ended(at(2), owner).unwrap();
    let playback = f.start_playback(3);
    for us in [4, 20_004, 40_004, 60_004, 80_004] {
        f.exchange(us).unwrap();
    }
    // Later receipt carries an earlier driver retirement time. The current
    // evaluator checks raw ring_requested against that boundary with 50ms
    // tolerance; an ignored summary must not hide the actual 80,004us request.
    f.owner.playback_ended(at(82_004), playback).unwrap();
    record(
        &mut f.trace,
        json!({"kind":"speech_final_sample_retired",
        "at_us":25_000,"turn":owner.turn()}),
    )
    .unwrap();
    f.exchange(82_005).unwrap();
    let stale_tail = f
        .trace
        .iter()
        .find(|event| {
            event["kind"] == "ring_requested"
                && event["phase"] == "speaking"
                && event["at_us"]
                    .as_u64()
                    .is_some_and(|us| us > 25_000 + 50_000)
        })
        .expect("raw stale-tail evidence required by the existing evaluator");
    assert_eq!(stale_tail["at_us"], 80_004);
    assert_eq!(stale_tail["owner"], serde_json::to_value(owner).unwrap());
}

#[test]
fn ring_trace_tail_is_unacknowledged_original_request_and_keeps_other_indices() {
    let mut f = Fixture::new();
    let head = f.exchange(1).unwrap();
    let first = f.exchange(20_001).unwrap();
    let tail_index = f
        .trace
        .iter()
        .position(|event| event["trace_role"] == "renewal_tail")
        .unwrap();
    assert_eq!(
        f.trace[tail_index]["frame"],
        serde_json::to_value(first).unwrap()
    );
    let head_event = f.trace[0].clone();
    let marker_index = f.trace.len();
    let marker = json!({"kind":"speech_final_sample_retired","at_us":30_000,"turn":head.permit.owner().turn()});
    record(&mut f.trace, marker.clone()).unwrap();
    let pending = f.request(40_001);
    assert_eq!(f.trace[0], head_event);
    assert_eq!(f.trace[marker_index], marker);
    assert_eq!(f.trace[tail_index]["at_us"], pending.requested_at_us);
    assert_eq!(
        f.trace[tail_index]["frame"],
        serde_json::to_value(pending).unwrap()
    );
    assert!(f.trace[tail_index]["details"].is_null());
    assert_eq!(last_summary(&f.trace)["requested"], 2);
    assert_eq!(last_summary(&f.trace)["validated_presentations"], 1);
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 1);
    assert_eq!(
        head_requests(&f.trace) as u64 + sum(&f.trace, "requested"),
        f.requests
    );
}

#[test]
fn ring_trace_tail_slot_capacity_failure_is_fatal_and_reserves_run_end() {
    let mut f = Fixture::new();
    f.exchange(1).unwrap();
    f.trace
        .resize_with(MAX_TRACE_EVENTS - 2, || json!({"kind":"other_evidence"}));
    renew_input(&mut f.owner, 20_001);
    let error = f.tick(20_001).unwrap_err();
    assert!(error.to_string().contains("trace capacity reached"));
    assert_eq!(f.trace.len(), MAX_TRACE_EVENTS - 1);
    assert_eq!(last_summary(&f.trace)["requested"], 1);
    assert_eq!(last_summary(&f.trace)["unacknowledged_requests"], 1);
    assert_eq!(last_summary(&f.trace)["validated_presentations"], 0);
    // Same reserved terminal slot used by run_source after any runtime error.
    f.trace
        .push(json!({"kind":"run_end", "status":"failed", "error":error.to_string()}));
    assert_eq!(f.trace.len(), MAX_TRACE_EVENTS);
}
