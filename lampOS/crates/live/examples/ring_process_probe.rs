//! Local process/IPC qualification with a memory-only sink and synthetic state.
//! No SPI, microphone, speaker, provider, camera or motor is opened.
use lamp_interaction::{
    AdmissionState, AdmittedInput, CaptureState, Controller, MonoTime, OutputKind, Permission,
    TurnOwner,
};
use lamp_ipc::monotonic_us;
use lamp_live::{
    choreography::RingChoreographer,
    process::{SessionDirectory, Worker, new_boot, parse_boot},
    ring_wire::{RingBlankReason, RingFeedback},
    ring_worker::run_with_sink,
    transport::WorkerChannels,
    wire::{Control, WorkerEvent},
};
use lamp_ring::{EncodedFrame, FrameSink};
use serde_json::json;
use std::{
    io,
    path::Path,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

struct MemoryOnlySink;
impl FrameSink for MemoryOnlySink {
    fn write_frame(&mut self, frame: &EncodedFrame) -> io::Result<()> {
        std::hint::black_box(frame.bytes());
        Ok(())
    }
}

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

fn main() -> Result<()> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    match arguments.as_slice() {
        [command, role, directory, controller, worker] if command == "worker" && role == "ring" => {
            let controller = parse_boot(controller)?;
            let worker = parse_boot(worker)?;
            let mut channels =
                WorkerChannels::bind(Path::new(directory), role, controller, worker, true)?;
            channels.connect(Path::new(directory), role, true)?;
            run_with_sink(channels, controller, MemoryOnlySink)?;
            Ok(())
        }
        [] => probe(120),
        [flag, count] if flag == "--samples" => probe(count.parse()?),
        _ => Err(io::Error::other("usage: ring_process_probe [--samples 6..256]").into()),
    }
}

fn retain_input(controller: &mut Controller) -> Result<()> {
    let at = now();
    let until = at
        .checked_add(100_000)
        .ok_or_else(|| io::Error::other("clock overflow"))?;
    controller.set_capture(at, CaptureState::RetainingUntil(until))?;
    controller.set_admission(at, AdmissionState::OpenUntil(until))?;
    Ok(())
}

fn probe(count: usize) -> Result<()> {
    if !(6..=256).contains(&count) {
        return Err(io::Error::other("sample count must be 6..256").into());
    }
    let directory = SessionDirectory::create()?;
    let boot = new_boot()?;
    let startup_at = monotonic_us();
    let mut worker = Worker::spawn(
        &std::env::current_exe()?,
        &directory.path,
        "ring",
        boot,
        &[],
    )?;
    let startup_us = monotonic_us() - startup_at;
    let mut controller = Controller::new(boot, now());
    controller.set_microphone_permission(now(), Permission::Allowed)?;
    let mut choreography = RingChoreographer::new(24)?;
    let mut turn: Option<TurnOwner> = None;
    let mut measurements = Vec::with_capacity(count);
    let mut refresh_at = 0;
    for sample in 0..count {
        retain_input(&mut controller)?;
        match sample % 6 {
            0 => turn = Some(controller.admit(now(), AdmittedInput::NewTurn)?),
            2 => controller.input_ended(now(), turn.ok_or_else(|| io::Error::other("no turn"))?)?,
            4 => {
                // Simulated accepted playback, not audio delivery evidence.
                let plan = controller.plan_output(
                    now(),
                    turn.ok_or_else(|| io::Error::other("no turn"))?,
                    OutputKind::Speech,
                    None,
                )?;
                let permit = controller.issue_output(now(), plan, 100_000)?;
                controller.playback_started(now(), permit)?;
            }
            _ => {}
        }
        worker.channels.control.send(Control::Authority {
            snapshot: controller.snapshot(now())?,
        })?;
        let deadline = Instant::now() + Duration::from_millis(250);
        loop {
            if Instant::now() >= deadline {
                return Err(io::Error::other("finite process probe deadline exceeded").into());
            }
            let clock = monotonic_us();
            if clock.saturating_sub(refresh_at) >= 20_000 {
                retain_input(&mut controller)?;
                worker.channels.control.send(Control::Authority {
                    snapshot: controller.snapshot(now())?,
                })?;
                refresh_at = clock;
            }
            let mut presented = false;
            for _ in 0..16 {
                let Some(event) = worker.channels.control.receive::<WorkerEvent>()? else {
                    break;
                };
                match event {
                    WorkerEvent::Ring { report } => {
                        choreography.feedback(&report, controller.snapshot(now())?, now())?;
                        if let RingFeedback::Presented {
                            requested_at_us,
                            write_finished_at_us,
                            ..
                        } = report
                        {
                            measurements.push(write_finished_at_us - requested_at_us);
                            presented = true;
                        }
                    }
                    other => {
                        return Err(
                            io::Error::other(format!("unexpected probe event: {other:?}")).into(),
                        );
                    }
                }
            }
            if presented {
                break;
            }
            if let Some(frame) = choreography.prepare(&mut controller, now())? {
                worker.channels.control.send(Control::Authority {
                    snapshot: frame.snapshot,
                })?;
                worker.channels.data.send(frame)?;
            }
            worker.check_running()?;
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    controller.set_microphone_permission(now(), Permission::Denied)?;
    worker.channels.control.send(Control::Authority {
        snapshot: controller.snapshot(now())?,
    })?;
    let stop_requested_at_us = monotonic_us();
    worker.channels.control.send(Control::Stop)?;
    let deadline = Instant::now() + Duration::from_millis(250);
    loop {
        if Instant::now() >= deadline {
            return Err(
                io::Error::other("probe missing explicit shutdown black-write receipt").into(),
            );
        }
        match worker.channels.control.receive::<WorkerEvent>()? {
            Some(WorkerEvent::Ring {
                report:
                    report @ RingFeedback::Blanked {
                        reason: RingBlankReason::Shutdown,
                        ..
                    },
            }) => {
                if !report.confirms_shutdown(stop_requested_at_us, monotonic_us()) {
                    return Err(io::Error::other(
                        "probe received invalid shutdown write timestamps",
                    )
                    .into());
                }
                break;
            }
            Some(WorkerEvent::Ring {
                report: RingFeedback::Blanked { .. },
            })
            | None => {}
            other => {
                return Err(
                    io::Error::other(format!("unexpected probe shutdown: {other:?}")).into(),
                );
            }
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    worker.shutdown()?;
    let first_us = measurements[0];
    let ordered = measurements.clone();
    let mut warm = measurements[1..].to_vec();
    warm.sort_unstable();
    let warm_count = warm.len();
    measurements.sort_unstable();
    println!(
        "{}",
        json!({
            "status":"completed_host_process_probe",
            "samples":count,
            "boundary":"controller request to memory-only sink write completion in a separate process",
            "worker_startup_us":startup_us,
            "first_us":first_us,
            "warm_samples":warm_count,
            "warm_p50_us":warm[(warm_count * 50).div_ceil(100)-1],
            "warm_p95_us":warm[(warm_count * 95).div_ceil(100)-1],
            "warm_max_us":warm[warm_count-1],
            "p50_us":measurements[(count * 50).div_ceil(100)-1],
            "p95_us":measurements[(count * 95).div_ceil(100)-1],
            "p99_us":measurements[(count * 99).div_ceil(100)-1],
            "max_us":measurements[count-1],
            "ordered_us":ordered,
            "physical_devices_opened":false,
            "optical_qualification":false,
            "synthetic_conversation_state":true,
            "shutdown_black_write_received":true
        })
    );
    Ok(())
}
