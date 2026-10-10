//! Deterministic retention/ownership tests, not acoustic or classifier evidence.
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CaptureState, Controller, MonoTime,
    OutputKind, OutputPermit, Permission, PlaybackToken, TurnOwner,
};
use lamp_live::{
    activity::{Activity, ObservedAudio},
    admission::{
        AcceptanceBasis, Accepted, Candidate, CaptureLineage, Context, Decision, Evidence,
        InputAdmission, RejectReason, Step, Update, Verdict,
    },
};

const START: u64 = 1_000_000;

fn at(micros: u64) -> MonoTime {
    MonoTime::from_micros(micros)
}

fn boot(value: u8) -> BootId {
    BootId::new([value; 16]).unwrap()
}

struct Playing {
    controller: Controller,
    owner: TurnOwner,
    speech: OutputPermit,
    light: OutputPermit,
    playback: PlaybackToken,
    capture: CaptureLineage,
}

impl Playing {
    fn new() -> Self {
        Self::with_boot(1)
    }

    fn with_boot(value: u8) -> Self {
        let mut controller = Controller::new(boot(value), at(START - 400_000));
        controller
            .set_microphone_permission(at(START - 400_000), Permission::Allowed)
            .unwrap();
        controller
            .set_capture(
                at(START - 400_000),
                CaptureState::RetainingUntil(at(START + 500_000)),
            )
            .unwrap();
        controller
            .set_admission(
                at(START - 400_000),
                AdmissionState::OpenUntil(at(START + 500_000)),
            )
            .unwrap();
        let owner = controller
            .admit(at(START - 10_000), AdmittedInput::NewTurn)
            .unwrap();
        controller.input_ended(at(START - 9_000), owner).unwrap();
        let plan = controller
            .plan_output(at(START), owner, OutputKind::Speech, None)
            .unwrap();
        let speech = controller.issue_output(at(START), plan, 250_000).unwrap();
        let playback = controller.playback_started(at(START), speech).unwrap();
        let light_plan = controller
            .plan_output(at(START), owner, OutputKind::Light, None)
            .unwrap();
        let light = controller
            .issue_output(at(START), light_plan, 250_000)
            .unwrap();
        let snapshot = controller.snapshot(at(START)).unwrap();
        Self {
            controller,
            owner,
            speech,
            light,
            playback,
            capture: CaptureLineage {
                worker: boot(value + 1),
                epoch: 1,
                dsp_epoch: 1,
                privacy_generation: snapshot.microphone_generation(),
            },
        }
    }

    fn context(&mut self, now: u64) -> Context {
        Context {
            capture: self.capture,
            authority: self.controller.snapshot(at(now)).unwrap(),
        }
    }

    fn assert_old_output_unchanged(&mut self, now: u64) {
        let state = self.controller.snapshot(at(now)).unwrap();
        assert_eq!(state.owner(), Some(self.owner));
        assert_eq!(state.playback(), Some(self.playback));
        let mut guard = BoundaryGuard::new(state.boot(), at(now));
        guard.install(at(now), state).unwrap();
        guard
            .check(at(now), self.speech, OutputKind::Speech)
            .unwrap();
        guard.check(at(now), self.light, OutputKind::Light).unwrap();
    }

    fn finish_old_reply(&mut self, now: u64) -> Context {
        self.controller
            .playback_ended(at(now), self.playback)
            .unwrap();
        self.controller.complete_turn(at(now), self.owner).unwrap();
        self.context(now)
    }
}

fn audio(sequence: u64, captured_at_us: u64) -> ObservedAudio {
    ObservedAudio {
        sequence,
        captured_at_us,
        samples: std::array::from_fn(|sample| {
            (sequence as i16)
                .wrapping_mul(127)
                .wrapping_add(sample as i16)
        }),
    }
}

fn prefix(first_sequence: u64, count: usize, trigger_at: u64) -> Vec<ObservedAudio> {
    (0..count)
        .map(|index| {
            audio(
                first_sequence + index as u64,
                trigger_at - (count - 1 - index) as u64 * 10_000,
            )
        })
        .collect()
}

fn start(
    gate: &mut InputAdmission,
    context: Context,
    original: &[ObservedAudio],
    now: u64,
) -> Candidate {
    let update = gate
        .observe(Activity::Start(original.to_vec()), context, now)
        .unwrap();
    assert!(update.rejection.is_none());
    let Step::Candidate(candidate) = update.step else {
        panic!("expected retained candidate, got {:?}", update.step)
    };
    candidate
}

fn accept(candidate: Candidate, through_sequence: u64, now: u64) -> Evidence {
    Evidence {
        candidate: candidate.id,
        through_sequence,
        produced_at_us: now,
        verdict: Verdict::Accept(AcceptanceBasis::DirectedSessionVadOnly),
    }
}

fn accepted(decision: Decision) -> Accepted {
    let Decision::Accepted(accepted) = decision else {
        panic!("expected acceptance, got {decision:?}")
    };
    accepted
}

fn no_output(update: Update) {
    assert!(update.rejection.is_none());
    assert!(matches!(update.step, Step::None), "{:?}", update.step);
}

fn same_audio(actual: &[ObservedAudio], expected: &[ObservedAudio]) {
    assert_eq!(actual.len(), expected.len());
    for (actual, expected) in actual.iter().zip(expected) {
        assert_eq!(actual.sequence, expected.sequence);
        assert_eq!(actual.captured_at_us, expected.captured_at_us);
        assert_eq!(actual.samples, expected.samples);
    }
}

#[test]
fn pending_and_policy_rejection_do_not_cancel_real_speech_or_light() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    assert_eq!(candidate.displaced_owner, Some(playing.owner));
    playing.assert_old_output_unchanged(START);
    no_output(
        gate.observe(
            Activity::Continue(audio(31, START + 10_000)),
            playing.context(START + 10_000),
            START + 10_000,
        )
        .unwrap(),
    );
    assert!(
        gate.maintain(playing.context(START + 20_000), START + 20_000)
            .unwrap()
            .is_none()
    );
    playing.assert_old_output_unchanged(START + 20_000);
    let evidence = Evidence {
        verdict: Verdict::Reject,
        ..accept(candidate, 31, START + 20_000)
    };
    let decision = gate
        .decide(evidence, playing.context(START + 20_000), START + 20_000)
        .unwrap();
    assert!(matches!(decision, Decision::Rejected(rejected)
        if rejected.candidate == candidate.id && rejected.reason == RejectReason::Policy));
    playing.assert_old_output_unchanged(START + 20_000);
}

#[test]
fn acceptance_drains_exact_original_prefix_and_held_pcm_once() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let mut original = prefix(1, 30, START);
    let candidate = start(&mut gate, playing.context(START), &original, START);
    for index in 1..=3 {
        let block = audio(30 + index, START + index * 10_000);
        no_output(
            gate.observe(
                Activity::Continue(block.clone()),
                playing.context(block.captured_at_us),
                block.captured_at_us,
            )
            .unwrap(),
        );
        original.push(block);
    }
    let evidence = accept(candidate, 33, START + 30_000);
    let result = accepted(
        gate.decide(evidence, playing.context(START + 30_000), START + 30_000)
            .unwrap(),
    );
    assert_eq!(result.candidate.id, candidate.id);
    assert_eq!(result.basis, AcceptanceBasis::DirectedSessionVadOnly);
    assert!(!result.ended);
    same_audio(&result.audio, &original);
    // Only the caller can now transfer ownership; acceptance itself cannot.
    playing.assert_old_output_unchanged(START + 30_000);
    assert!(matches!(
        gate.decide(evidence, playing.context(START + 30_000), START + 30_000)
            .unwrap(),
        Decision::Ignored
    ));
    let next = audio(34, START + 40_000);
    let update = gate
        .observe(
            Activity::Continue(next.clone()),
            playing.context(next.captured_at_us),
            next.captured_at_us,
        )
        .unwrap();
    assert!(update.rejection.is_none());
    let Step::Audio(actual) = update.step else {
        panic!("accepted input did not stream")
    };
    same_audio(&[actual], &[next]);
    let end = audio(35, START + 50_000);
    let update = gate
        .observe(
            Activity::End(end.clone()),
            playing.context(end.captured_at_us),
            end.captured_at_us,
        )
        .unwrap();
    let Step::End(actual) = update.step else {
        panic!("accepted endpoint missing")
    };
    same_audio(&[actual], &[end]);
    no_output(
        gate.observe(
            Activity::Quiet,
            playing.context(START + 60_000),
            START + 60_000,
        )
        .unwrap(),
    );
}

#[test]
fn early_endpoint_is_retained_and_delivered_once_even_after_quiet_ticks() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let mut original = prefix(1, 6, START);
    let candidate = start(&mut gate, playing.context(START), &original, START);
    let end = audio(7, START + 10_000);
    no_output(
        gate.observe(
            Activity::End(end.clone()),
            playing.context(end.captured_at_us),
            end.captured_at_us,
        )
        .unwrap(),
    );
    original.push(end);
    no_output(
        gate.observe(
            Activity::Quiet,
            playing.context(START + 20_000),
            START + 20_000,
        )
        .unwrap(),
    );
    let evidence = accept(candidate, 7, START + 20_000);
    let result = accepted(
        gate.decide(evidence, playing.context(START + 20_000), START + 20_000)
            .unwrap(),
    );
    assert!(result.ended);
    same_audio(&result.audio, &original);
    assert!(matches!(
        gate.decide(evidence, playing.context(START + 20_000), START + 20_000)
            .unwrap(),
        Decision::Ignored
    ));
    let next = start(
        &mut gate,
        playing.context(START + 30_000),
        &prefix(8, 1, START + 30_000),
        START + 30_000,
    );
    assert_ne!(candidate.id, next.id);
}

#[test]
fn uncorrelated_future_stale_and_pretrigger_evidence_are_inert() {
    for invalid_case in 0..7 {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let candidate = start(
            &mut gate,
            playing.context(START),
            &prefix(1, 30, START),
            START,
        );
        let now = START + if invalid_case == 1 { 100_000 } else { 10_000 };
        let mut evidence = accept(candidate, 30, now);
        match invalid_case {
            0 => {
                let mut other = Playing::with_boot(9);
                evidence.candidate = start(
                    &mut InputAdmission::default(),
                    other.context(START),
                    &prefix(1, 30, START),
                    START,
                )
                .id;
            }
            1 => {} // A fresh receipt cannot freshen a 100 ms old source frame.
            2 => evidence.produced_at_us = now + 1,
            3 => evidence.through_sequence = 29,
            4 => evidence.through_sequence = 31,
            5 => evidence.produced_at_us = START - 1,
            6 => evidence.through_sequence = 0,
            _ => unreachable!(),
        }
        assert!(
            matches!(
                gate.decide(evidence, playing.context(now), now).unwrap(),
                Decision::Ignored
            ),
            "case {invalid_case}"
        );
        playing.assert_old_output_unchanged(now);
        no_output(
            gate.observe(
                Activity::Continue(audio(31, START + 10_000)),
                playing.context(now),
                now,
            )
            .unwrap(),
        );
        let result = accepted(
            gate.decide(accept(candidate, 31, now), playing.context(now), now)
                .unwrap(),
        );
        assert_eq!(
            result.audio.len(),
            31,
            "invalid evidence consumed the candidate"
        );
    }
}

#[test]
fn fixed_deadline_is_anchored_to_capture_not_receipt_or_new_evidence() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START + 80_000),
        &prefix(1, 30, START),
        START + 80_000,
    );
    assert_eq!(candidate.decision_deadline_us, START + 200_000);
    for (index, captured) in [40_000, 80_000, 120_000, 160_000, 195_000]
        .into_iter()
        .enumerate()
    {
        let now = START + captured.max(90_000);
        no_output(
            gate.observe(
                Activity::Continue(audio(31 + index as u64, START + captured)),
                playing.context(now),
                now,
            )
            .unwrap(),
        );
    }
    assert!(
        gate.maintain(playing.context(START + 199_999), START + 199_999)
            .unwrap()
            .is_none()
    );
    let decision = gate
        .decide(
            accept(candidate, 35, START + 200_000),
            playing.context(START + 200_000),
            START + 200_000,
        )
        .unwrap();
    assert!(matches!(decision, Decision::Rejected(rejected)
        if rejected.candidate == candidate.id && rejected.reason == RejectReason::Deadline));
    playing.assert_old_output_unchanged(START + 200_000);
}

fn capacity_case(prefix_blocks: usize, overflow: bool) {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let mut expected = prefix(1, prefix_blocks, START);
    let candidate = start(&mut gate, playing.context(START), &expected, START);
    // Batched host reads can share close timestamps. This reaches the PCM cap
    // while the independent wall-clock deadline is still clearly open.
    for index in 1..=20 {
        let block = audio(prefix_blocks as u64 + index, START + index * 1_000);
        no_output(
            gate.observe(
                Activity::Continue(block.clone()),
                playing.context(block.captured_at_us),
                block.captured_at_us,
            )
            .unwrap(),
        );
        expected.push(block);
    }
    if overflow {
        let update = gate
            .observe(
                Activity::Continue(audio(prefix_blocks as u64 + 21, START + 21_000)),
                playing.context(START + 21_000),
                START + 21_000,
            )
            .unwrap();
        assert!(update.rejection.is_none());
        assert!(matches!(update.step, Step::Rejected(rejected)
            if rejected.candidate == candidate.id && rejected.reason == RejectReason::Capacity));
        assert!(matches!(
            gate.decide(
                accept(candidate, prefix_blocks as u64 + 20, START + 21_000),
                playing.context(START + 21_000),
                START + 21_000
            )
            .unwrap(),
            Decision::Ignored
        ));
    } else {
        let result = accepted(
            gate.decide(
                accept(candidate, prefix_blocks as u64 + 20, START + 20_000),
                playing.context(START + 20_000),
                START + 20_000,
            )
            .unwrap(),
        );
        same_audio(&result.audio, &expected);
        assert!(result.audio.len() <= 50);
    }
    playing.assert_old_output_unchanged(START + 21_000);
}

#[test]
fn full_prefix_plus_twenty_held_blocks_is_the_fifty_block_limit() {
    capacity_case(30, false);
    capacity_case(30, true);
}

#[test]
fn short_prefix_cannot_borrow_unused_prefix_capacity_to_wait_longer() {
    capacity_case(6, false);
    capacity_case(6, true);
}

#[test]
fn rejection_drains_the_same_burst_until_its_endpoint_before_rearming() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    let evidence = Evidence {
        verdict: Verdict::Reject,
        ..accept(candidate, 30, START)
    };
    assert!(matches!(
        gate.decide(evidence, playing.context(START), START)
            .unwrap(),
        Decision::Rejected(_)
    ));
    for index in 1..=3 {
        let now = START + index * 10_000;
        no_output(
            gate.observe(
                Activity::Continue(audio(30 + index, now)),
                playing.context(now),
                now,
            )
            .unwrap(),
        );
        assert!(matches!(
            gate.decide(
                accept(candidate, 30 + index, now),
                playing.context(now),
                now
            )
            .unwrap(),
            Decision::Ignored
        ));
        playing.assert_old_output_unchanged(now);
    }
    no_output(
        gate.observe(
            Activity::End(audio(34, START + 40_000)),
            playing.context(START + 40_000),
            START + 40_000,
        )
        .unwrap(),
    );
    let next = start(
        &mut gate,
        playing.context(START + 50_000),
        &prefix(35, 1, START + 50_000),
        START + 50_000,
    );
    assert_ne!(candidate.id, next.id);
}

#[test]
fn expired_ended_candidate_and_a_new_start_are_both_reported() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let first = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 6, START),
        START,
    );
    no_output(
        gate.observe(
            Activity::End(audio(7, START + 10_000)),
            playing.context(START + 10_000),
            START + 10_000,
        )
        .unwrap(),
    );
    let update = gate
        .observe(
            Activity::Start(prefix(8, 1, START + 200_000)),
            playing.context(START + 200_000),
            START + 200_000,
        )
        .unwrap();
    let rejection = update.rejection.expect("old decision expiry was lost");
    assert_eq!(rejection.candidate, first.id);
    assert_eq!(rejection.reason, RejectReason::Deadline);
    let Step::Candidate(next) = update.step else {
        panic!("new start was swallowed")
    };
    assert_ne!(next.id, first.id);
    let result = accepted(
        gate.decide(
            accept(next, 8, START + 200_000),
            playing.context(START + 200_000),
            START + 200_000,
        )
        .unwrap(),
    );
    same_audio(&result.audio, &[audio(8, START + 200_000)]);
}

#[test]
fn malformed_prefixes_never_grant_a_candidate_or_affect_old_output() {
    for kind in 0..9 {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let mut original = prefix(1, 30, START);
        let mut now = START;
        match kind {
            0 => original.clear(),
            1 => original = prefix(1, 31, START),
            2 => original[0].sequence = 0,
            3 => original[0].captured_at_us = 0,
            4 => original[10].sequence += 1,
            5 => original[10].captured_at_us = original[9].captured_at_us - 1,
            6 => original[10].captured_at_us = original[9].captured_at_us + 50_001,
            7 => original[29].captured_at_us = START + 1,
            8 => now += 100_000,
            _ => unreachable!(),
        }
        assert!(
            gate.observe(Activity::Start(original), playing.context(now), now)
                .is_err(),
            "kind {kind}"
        );
        playing.assert_old_output_unchanged(now);
    }
}

#[test]
fn a_capture_gap_invalidates_pending_audio_and_makes_late_acceptance_inert() {
    for kind in 0..5 {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let candidate = start(
            &mut gate,
            playing.context(START),
            &prefix(1, 30, START),
            START,
        );
        let mut next = audio(31, START + 10_000);
        match kind {
            0 => next.sequence = 30,
            1 => next.sequence = 32,
            2 => next.captured_at_us = START - 1,
            3 => next.captured_at_us = START + 50_001,
            4 => next.captured_at_us = START + 60_001,
            _ => unreachable!(),
        }
        let now = START + 60_000;
        assert!(
            gate.observe(Activity::Continue(next), playing.context(now), now)
                .is_err(),
            "kind {kind}"
        );
        assert!(matches!(
            gate.decide(accept(candidate, 30, now), playing.context(now), now)
                .unwrap(),
            Decision::Ignored
        ));
        playing.assert_old_output_unchanged(now);
    }
}

#[test]
fn pending_capture_and_privacy_incarnations_cannot_be_relabelled() {
    for kind in 0..6 {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let candidate = start(
            &mut gate,
            playing.context(START),
            &prefix(1, 30, START),
            START,
        );
        let mut changed = playing.context(START + 10_000);
        let expected = match kind {
            0 => {
                changed.capture.worker = boot(77);
                RejectReason::CaptureChanged
            }
            1 => {
                changed.capture.epoch += 1;
                RejectReason::CaptureChanged
            }
            2 => {
                changed.capture.dsp_epoch += 1;
                RejectReason::CaptureChanged
            }
            3 => {
                changed.capture.privacy_generation += 1;
                RejectReason::InputUnavailable
            }
            4 => {
                changed.capture.epoch = 0;
                RejectReason::InputUnavailable
            }
            5 => {
                changed.capture.dsp_epoch = 0;
                RejectReason::InputUnavailable
            }
            _ => unreachable!(),
        };
        let rejection = gate.maintain(changed, START + 10_000).unwrap().unwrap();
        assert_eq!(rejection.candidate, candidate.id);
        assert_eq!(rejection.reason, expected);
        assert!(matches!(
            gate.decide(
                accept(candidate, 30, START + 10_000),
                playing.context(START + 10_000),
                START + 10_000
            )
            .unwrap(),
            Decision::Ignored
        ));
        playing.assert_old_output_unchanged(START + 10_000);
    }
}

#[test]
fn closed_privacy_and_expired_authority_reject_pending_input() {
    for close_privacy in [false, true] {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let context = playing.context(START);
        let candidate = start(&mut gate, context, &prefix(1, 30, START), START);
        let (context, now) = if close_privacy {
            playing
                .controller
                .set_microphone_permission(at(START + 10_000), Permission::Denied)
                .unwrap();
            (playing.context(START + 10_000), START + 10_000)
        } else {
            (context, START + 250_000)
        };
        let decision = gate
            .decide(accept(candidate, 30, now), context, now)
            .unwrap();
        assert!(matches!(decision, Decision::Rejected(rejected)
            if rejected.candidate == candidate.id && rejected.reason == RejectReason::InputUnavailable));
    }
}

#[test]
fn a_newer_owner_cannot_be_cancelled_by_an_older_candidate_decision() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    let now = START + 10_000;
    let newer = playing
        .controller
        .admit(at(now), AdmittedInput::NewTurn)
        .unwrap();
    let plan = playing
        .controller
        .plan_output(at(now), newer, OutputKind::Light, None)
        .unwrap();
    let permit = playing
        .controller
        .issue_output(at(now), plan, 200_000)
        .unwrap();
    let context = playing.context(now);
    gate.owner_completed(playing.owner, context.authority, now)
        .unwrap();
    let result = gate
        .decide(accept(candidate, 30, now), context, now)
        .unwrap();
    assert!(
        matches!(result, Decision::Rejected(rejected) if rejected.reason == RejectReason::AuthorityChanged)
    );
    let unchanged = playing.controller.snapshot(at(now)).unwrap();
    assert_eq!(unchanged.owner(), Some(newer));
    let mut guard = BoundaryGuard::new(unchanged.boot(), at(now));
    guard.install(at(now), unchanged).unwrap();
    guard.check(at(now), permit, OutputKind::Light).unwrap();
}

#[test]
fn controller_restart_rejects_but_camera_or_heartbeat_updates_do_not() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    playing
        .controller
        .set_camera_permission(at(START + 10_000), Permission::Allowed)
        .unwrap();
    let current = playing.context(START + 10_000);
    assert!(gate.maintain(current, START + 10_000).unwrap().is_none());
    playing.assert_old_output_unchanged(START + 10_000);
    let mut other = Playing::with_boot(9);
    let changed = Context {
        capture: playing.capture,
        authority: other.context(START + 20_000).authority,
    };
    let result = gate
        .decide(
            accept(candidate, 30, START + 20_000),
            changed,
            START + 20_000,
        )
        .unwrap();
    assert!(
        matches!(result, Decision::Rejected(rejected) if rejected.reason == RejectReason::AuthorityChanged)
    );
}

#[test]
fn explicit_natural_completion_allows_a_new_turn_without_rewriting_candidate_history() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    let now = START + 10_000;
    let completed = playing.finish_old_reply(now);
    gate.owner_completed(playing.owner, completed.authority, now)
        .unwrap();
    let result = accepted(
        gate.decide(accept(candidate, 30, now), completed, now)
            .unwrap(),
    );
    assert_eq!(result.candidate.displaced_owner, Some(playing.owner));
    assert_eq!(playing.controller.snapshot(at(now)).unwrap().owner(), None);
    let new_owner = playing
        .controller
        .admit(at(now), AdmittedInput::NewTurn)
        .unwrap();
    assert_ne!(new_owner, playing.owner);
    assert!(new_owner.turn() > playing.owner.turn());
    // Streaming capture belongs to the retained source; the explicit new owner
    // is valid even though it differs from the old reply named in the candidate.
    let update = gate
        .observe(
            Activity::Continue(audio(31, START + 20_000)),
            playing.context(START + 20_000),
            START + 20_000,
        )
        .unwrap();
    assert!(matches!(update.step, Step::Audio(_)));
}

#[test]
fn absent_owner_or_wrong_completion_receipt_cannot_imply_natural_completion() {
    for wrong_receipt in [false, true] {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let candidate = start(
            &mut gate,
            playing.context(START),
            &prefix(1, 30, START),
            START,
        );
        let now = START + 10_000;
        let completed = playing.finish_old_reply(now);
        if wrong_receipt {
            gate.owner_completed(Playing::with_boot(9).owner, completed.authority, now)
                .unwrap();
        }
        let result = gate
            .decide(accept(candidate, 30, now), completed, now)
            .unwrap();
        assert!(
            matches!(result, Decision::Rejected(rejected) if rejected.reason == RejectReason::AuthorityChanged)
        );
    }
}

#[test]
fn reset_never_reuses_candidate_identity_or_accepts_old_evidence() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let first = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    gate.reset();
    let second = start(
        &mut gate,
        playing.context(START + 10_000),
        &prefix(31, 1, START + 10_000),
        START + 10_000,
    );
    assert_ne!(first.id, second.id);
    let old = Evidence {
        through_sequence: 31,
        ..accept(first, 30, START + 10_000)
    };
    assert!(matches!(
        gate.decide(old, playing.context(START + 10_000), START + 10_000)
            .unwrap(),
        Decision::Ignored
    ));
    let result = accepted(
        gate.decide(
            accept(second, 31, START + 10_000),
            playing.context(START + 10_000),
            START + 10_000,
        )
        .unwrap(),
    );
    same_audio(&result.audio, &[audio(31, START + 10_000)]);
}

#[test]
fn streaming_requires_contiguous_fresh_audio_and_fixed_capture_privacy() {
    for kind in 0..9 {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let candidate = start(
            &mut gate,
            playing.context(START),
            &prefix(1, 30, START),
            START,
        );
        accepted(
            gate.decide(accept(candidate, 30, START), playing.context(START), START)
                .unwrap(),
        );
        let now = START + if kind == 8 { 110_000 } else { 60_000 };
        let mut context = playing.context(now);
        let mut next = audio(31, START + 10_000);
        match kind {
            0 => next.sequence = 32,
            1 => next.sequence = 30,
            2 => next.captured_at_us = START - 1,
            3 => next.captured_at_us = START + 50_001,
            4 => next.captured_at_us = now + 1,
            5 => context.capture.worker = boot(77),
            6 => context.capture.epoch += 1,
            7 => context.capture.privacy_generation += 1,
            8 => {} // 100 ms stale is not fresh streaming evidence.
            _ => unreachable!(),
        }
        assert!(
            gate.observe(Activity::Continue(next), context, now)
                .is_err(),
            "kind {kind}"
        );
        assert!(matches!(
            gate.decide(accept(candidate, 30, now), playing.context(now), now)
                .unwrap(),
            Decision::Ignored
        ));
    }
}

#[test]
fn planned_dsp_advance_preserves_stream_but_cannot_rewind_it() {
    let mut playing = Playing::new();
    let mut gate = InputAdmission::default();
    let candidate = start(
        &mut gate,
        playing.context(START),
        &prefix(1, 30, START),
        START,
    );
    accepted(
        gate.decide(accept(candidate, 30, START), playing.context(START), START)
            .unwrap(),
    );
    playing.capture.dsp_epoch = 2;
    let update = gate
        .observe(
            Activity::Continue(audio(31, START + 10_000)),
            playing.context(START + 10_000),
            START + 10_000,
        )
        .unwrap();
    assert!(matches!(update.step, Step::Audio(_)));
    playing.capture.dsp_epoch = 1;
    assert!(
        gate.observe(
            Activity::Continue(audio(32, START + 20_000)),
            playing.context(START + 20_000),
            START + 20_000
        )
        .is_err()
    );
}

#[test]
fn detector_failure_or_clock_regression_cannot_leave_acceptable_pending_audio() {
    for regress_clock in [false, true] {
        let mut playing = Playing::new();
        let mut gate = InputAdmission::default();
        let context = playing.context(START);
        let candidate = start(&mut gate, context, &prefix(1, 30, START), START);
        if regress_clock {
            assert!(gate.maintain(context, START - 1).is_err());
        } else {
            assert!(
                gate.observe(Activity::Fault("test capture failure"), context, START)
                    .is_err()
            );
        }
        assert!(matches!(
            gate.decide(accept(candidate, 30, START), playing.context(START), START)
                .unwrap(),
            Decision::Ignored
        ));
        playing.assert_old_output_unchanged(START);
    }
}
