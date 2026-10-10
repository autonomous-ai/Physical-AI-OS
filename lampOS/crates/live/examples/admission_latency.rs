//! Host mechanism timing with synthetic PCM. No VAD, I/O, cloud or device access.
use lamp_interaction::{AdmissionState, BootId, CaptureState, Controller, MonoTime, Permission};
use lamp_live::{
    activity::{Activity, ObservedAudio},
    admission::{
        AcceptanceBasis, CaptureLineage, Context, Decision, Evidence, InputAdmission, Step, Verdict,
    },
};
use std::{hint::black_box, time::Instant};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let boot = BootId::new([1; 16])?;
    let worker = BootId::new([2; 16])?;
    let mut controller = Controller::new(boot, MonoTime::from_micros(1));
    controller.set_microphone_permission(MonoTime::from_micros(2), Permission::Allowed)?;
    let mut admission = InputAdmission::default();
    let mut timings = Vec::with_capacity(4096);
    for trial in 0..4096u64 {
        let at_us = 1_000_000 + trial * 1_000_000;
        let now = MonoTime::from_micros(at_us);
        let until = MonoTime::from_micros(at_us + 100_000);
        controller.set_capture(now, CaptureState::RetainingUntil(until))?;
        controller.set_admission(now, AdmissionState::OpenUntil(until))?;
        let authority = controller.snapshot(now)?;
        let context = Context {
            capture: CaptureLineage {
                worker,
                epoch: 1,
                dsp_epoch: 1,
                privacy_generation: authority.microphone_generation(),
            },
            authority,
        };
        let audio = (0..30u64)
            .map(|i| ObservedAudio {
                sequence: trial * 30 + i + 1,
                captured_at_us: at_us - (29 - i) * 10_000,
                samples: [i as i16; 160],
            })
            .collect();
        let start = Instant::now();
        let Step::Candidate(candidate) = admission
            .observe(Activity::Start(audio), context, at_us)?
            .step
        else {
            return Err("synthetic candidate not retained".into());
        };
        let decision = admission.decide(
            Evidence {
                candidate: candidate.id,
                through_sequence: candidate.trigger_sequence,
                produced_at_us: at_us,
                verdict: Verdict::Accept(AcceptanceBasis::DirectedSessionVadOnly),
            },
            context,
            at_us,
        )?;
        black_box(&decision);
        timings.push(start.elapsed().as_nanos());
        let Decision::Accepted(accepted) = decision else {
            return Err("synthetic decision failed".into());
        };
        assert_eq!(accepted.audio.len(), 30);
        assert_eq!(accepted.audio[0].captured_at_us, at_us - 290_000);
        admission.reset();
    }
    let first = timings[0];
    timings.sort_unstable();
    let quantile = |q: usize| timings[(timings.len() * q).div_ceil(100) - 1];
    println!(
        "{}",
        serde_json::json!({
            "boundary":"candidate creation + immediate directed decision; host synthetic PCM",
            "excludes":"VAD, prefix preparation, result destruction, IPC, provider, device and acoustic output",
            "samples":timings.len(), "first_ns":first,
            "p50_ns":quantile(50), "p95_ns":quantile(95), "p99_ns":quantile(99), "max_ns":timings.last(),
            "target_p99_ns":100_000, "target_met":quantile(99) <= 100_000,
            "physical_qualification":false,
        })
    );
    Ok(())
}
