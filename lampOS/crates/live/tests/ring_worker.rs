use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, CaptureState, Controller, MonoTime, OutputKind,
    Permission, TurnOwner,
};
use lamp_ipc::monotonic_us;
use lamp_live::{
    process::{SessionDirectory, new_boot},
    ring_wire::{RingBlankReason, RingFeedback, RingFrame, RingPhase, RingRejection},
    ring_worker::{RingLink, run_with_sink},
    transport::WorkerChannels,
    wire::{Control, WorkerEvent, decode, encode},
};
use lamp_ring::{EncodedFrame, FrameSink};
use std::{
    collections::VecDeque,
    io,
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

#[derive(Default)]
struct Writes {
    frames: Vec<EncodedFrame>,
    fail_at: Vec<usize>,
}
#[derive(Clone)]
struct Sink(Arc<Mutex<Writes>>);
impl FrameSink for Sink {
    fn write_frame(&mut self, frame: &EncodedFrame) -> io::Result<()> {
        let mut writes = self.0.lock().unwrap();
        writes.frames.push(frame.clone());
        let call = writes.frames.len();
        if writes.fail_at.contains(&call) {
            return Err(io::Error::other(format!("injected write {call}")));
        }
        Ok(())
    }
}

struct SocketLink {
    channels: WorkerChannels,
    parent: Arc<Mutex<WorkerChannels>>,
    after_receive: Arc<Mutex<VecDeque<Control>>>,
}
impl RingLink for SocketLink {
    fn receive_control(&mut self) -> io::Result<Option<Control>> {
        self.channels.control.receive()
    }
    fn receive_frame(&mut self) -> io::Result<Option<RingFrame>> {
        let frame = self.channels.data.receive()?;
        if frame.is_some() {
            for control in self.after_receive.lock().unwrap().drain(..) {
                self.parent.lock().unwrap().control.send(control)?;
            }
        }
        Ok(frame)
    }
    fn publish(&mut self, event: WorkerEvent) -> io::Result<()> {
        self.channels.control.send(event)
    }
}

struct Rig {
    _directory: SessionDirectory,
    boot: BootId,
    parent: Arc<Mutex<WorkerChannels>>,
    writes: Arc<Mutex<Writes>>,
    after_receive: Arc<Mutex<VecDeque<Control>>>,
    completion: mpsc::Receiver<io::Result<()>>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Rig {
    fn start(fail_at: &[usize]) -> Self {
        let directory = SessionDirectory::create().unwrap();
        let boot = new_boot().unwrap();
        let child_boot = new_boot().unwrap();
        let mut parent =
            WorkerChannels::bind(&directory.path, "ring", boot, child_boot, false).unwrap();
        let mut channels =
            WorkerChannels::bind(&directory.path, "ring", boot, child_boot, true).unwrap();
        parent.connect(&directory.path, "ring", false).unwrap();
        channels.connect(&directory.path, "ring", true).unwrap();
        let parent = Arc::new(Mutex::new(parent));
        let writes = Arc::new(Mutex::new(Writes {
            fail_at: fail_at.to_vec(),
            ..Writes::default()
        }));
        let after_receive = Arc::new(Mutex::new(VecDeque::new()));
        let link = SocketLink {
            channels,
            parent: parent.clone(),
            after_receive: after_receive.clone(),
        };
        let sink = Sink(writes.clone());
        let (tx, completion) = mpsc::channel();
        let worker = thread::spawn(move || {
            let _ = tx.send(run_with_sink(link, boot, sink));
        });
        Self {
            _directory: directory,
            boot,
            parent,
            writes,
            after_receive,
            completion,
            worker: Some(worker),
        }
    }
    fn ready(&self) {
        assert!(matches!(self.event(), WorkerEvent::Ready));
        assert_eq!(self.writes.lock().unwrap().frames, [EncodedFrame::black()]);
    }
    fn event(&self) -> WorkerEvent {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if let Some(event) = self.parent.lock().unwrap().control.receive().unwrap() {
                return event;
            }
            assert!(Instant::now() < deadline, "finite worker feedback deadline");
            thread::sleep(Duration::from_millis(1));
        }
    }
    fn control(&self, command: Control) {
        self.parent.lock().unwrap().control.send(command).unwrap();
    }
    fn frame(&self, frame: RingFrame) {
        self.parent.lock().unwrap().data.send(frame).unwrap();
    }
    fn finish(&mut self) -> io::Result<()> {
        let result = self
            .completion
            .recv_timeout(Duration::from_secs(2))
            .unwrap();
        self.worker.take().unwrap().join().unwrap();
        result
    }
    fn colored(&self) -> usize {
        self.writes
            .lock()
            .unwrap()
            .frames
            .iter()
            .filter(|v| **v != EncodedFrame::black())
            .count()
    }
}
impl Drop for Rig {
    fn drop(&mut self) {
        if self.worker.is_some() {
            let _ = self.parent.lock().unwrap().control.send(Control::Stop);
            if self.completion.recv_timeout(Duration::from_secs(2)).is_ok() {
                let _ = self.worker.take().unwrap().join();
            }
        }
    }
}

fn conversation(boot: BootId) -> (Controller, TurnOwner) {
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    controller
        .set_capture(
            now(),
            CaptureState::RetainingUntil(now().checked_add(900_000).unwrap()),
        )
        .unwrap();
    controller
        .set_admission(
            now(),
            AdmissionState::OpenUntil(now().checked_add(900_000).unwrap()),
        )
        .unwrap();
    let owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    (controller, owner)
}
fn frame(controller: &mut Controller, owner: TurnOwner, lease_us: u64) -> RingFrame {
    let plan = controller
        .plan_output(now(), owner, OutputKind::Light, None)
        .unwrap();
    let permit = controller.issue_output(now(), plan, lease_us).unwrap();
    let snapshot = controller.snapshot(now()).unwrap();
    RingFrame {
        snapshot,
        permit,
        phase: RingPhase::from_snapshot(snapshot).unwrap(),
        ceiling: 40,
        requested_at_us: monotonic_us(),
    }
}
fn present(rig: &Rig, frame: RingFrame) {
    rig.control(Control::Authority {
        snapshot: frame.snapshot,
    });
    rig.frame(frame);
    assert!(
        matches!(rig.event(), WorkerEvent::Ring { report: RingFeedback::Presented { requested_at_us, .. } } if requested_at_us == frame.requested_at_us)
    );
}

#[test]
fn startup_stop_and_no_turn_never_invent_readiness_color() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let mut controller = Controller::new(rig.boot, now());
    let snapshot = controller.snapshot(now()).unwrap();
    assert_eq!(RingPhase::from_snapshot(snapshot), None);
    rig.control(Control::Authority { snapshot });
    rig.control(Control::Stop);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Blanked {
                reason: RingBlankReason::Shutdown,
                ..
            }
        }
    ));
    rig.finish().unwrap();
    assert_eq!(rig.colored(), 0);
}

#[test]
fn cancellation_arriving_after_real_data_receive_prevents_any_color() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    rig.control(Control::Authority {
        snapshot: original.snapshot,
    });
    controller.cancel_turn(now(), owner).unwrap();
    rig.after_receive
        .lock()
        .unwrap()
        .push_back(Control::Authority {
            snapshot: controller.snapshot(now()).unwrap(),
        });
    rig.frame(original);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Rejected {
                reason: RingRejection::StaleOwner,
                ..
            }
        }
    ));
    rig.control(Control::Stop);
    rig.finish().unwrap();
    assert_eq!(rig.colored(), 0);
}

#[test]
fn exhausted_shared_control_budget_defers_pending_frame_until_revocation() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    rig.control(Control::Authority {
        snapshot: original.snapshot,
    });
    controller.cancel_turn(now(), owner).unwrap();
    let revoked = controller.snapshot(now()).unwrap();
    {
        let mut controls = rig.after_receive.lock().unwrap();
        for _ in 0..16 {
            controls.push_back(Control::Authority {
                snapshot: original.snapshot,
            });
        }
        controls.push_back(Control::Authority { snapshot: revoked });
    }
    rig.frame(original);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Rejected {
                reason: RingRejection::StaleOwner,
                ..
            }
        }
    ));
    rig.control(Control::Stop);
    rig.finish().unwrap();
    assert_eq!(rig.colored(), 0);
}

#[test]
fn heartbeat_overtaking_data_keeps_original_request_and_permit() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    let newer = controller.snapshot(now()).unwrap();
    rig.control(Control::Authority { snapshot: newer });
    rig.frame(original);
    assert!(
        matches!(rig.event(), WorkerEvent::Ring { report: RingFeedback::Presented { requested_at_us, .. } } if requested_at_us == original.requested_at_us)
    );
    rig.control(Control::Stop);
    rig.finish().unwrap();
    assert_eq!(rig.colored(), 1);
}

#[test]
fn stale_owner_or_phase_cannot_blank_a_newer_valid_cue() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let listening = frame(&mut controller, owner, 200_000);
    controller.input_ended(now(), owner).unwrap();
    let waiting = frame(&mut controller, owner, 200_000);
    present(&rig, waiting);
    let count = rig.writes.lock().unwrap().frames.len();
    rig.control(Control::Authority {
        snapshot: listening.snapshot,
    });
    rig.frame(listening);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Rejected {
                reason: RingRejection::PhaseMismatch,
                ..
            }
        }
    ));
    assert_eq!(rig.writes.lock().unwrap().frames.len(), count);
    controller.cancel_turn(now(), owner).unwrap();
    let successor = controller
        .admit(now(), AdmittedInput::Interruption)
        .unwrap();
    let next = frame(&mut controller, successor, 200_000);
    rig.control(Control::Authority {
        snapshot: next.snapshot,
    });
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Blanked { .. }
        }
    ));
    rig.frame(next);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Presented { .. }
        }
    ));
    let count = rig.writes.lock().unwrap().frames.len();
    rig.frame(waiting);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Rejected {
                reason: RingRejection::StaleOwner,
                ..
            }
        }
    ));
    assert_eq!(rig.writes.lock().unwrap().frames.len(), count);
    rig.control(Control::Stop);
    rig.finish().unwrap();
}

#[test]
fn permit_expires_without_another_frame_then_stop_remains_available() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    present(&rig, frame(&mut controller, owner, 50_000));
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Blanked {
                reason: RingBlankReason::AuthorityLost {
                    reason: RingRejection::ExpiredPermit
                },
                ..
            }
        }
    ));
    rig.control(Control::Stop);
    rig.finish().unwrap();
    assert_eq!(rig.colored(), 1);
}

#[test]
fn repeated_stale_control_does_not_renew_authority_heartbeat() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    present(&rig, original);
    for _ in 0..8 {
        rig.control(Control::Authority {
            snapshot: original.snapshot,
        });
    }
    let error = rig.finish().unwrap_err();
    assert!(error.to_string().contains("heartbeat expired"));
    assert_eq!(rig.colored(), 1);
    assert_eq!(
        rig.writes.lock().unwrap().frames.last(),
        Some(&EncodedFrame::black())
    );
}

#[test]
fn newer_expired_privacy_revocation_blanks_before_old_allowed_lease_expires() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let allowed = frame(&mut controller, owner, 200_000);
    present(&rig, allowed);
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    let denied = controller.snapshot(now()).unwrap();
    // A well-shaped short successor lease can expire in transit while an
    // already-installed longer allowed snapshot is still fresh.
    let mut wire = serde_json::to_value(denied).unwrap();
    wire["expires_at"] = (denied.issued_at().as_micros() + 1).into();
    let expired = serde_json::from_value(wire).unwrap();
    rig.control(Control::Authority { snapshot: expired });
    let error = rig.finish().unwrap_err().to_string();
    assert!(
        error.contains(&lamp_interaction::Error::ExpiredState.to_string()),
        "{error}"
    );
    assert_eq!(rig.colored(), 1);
    assert_eq!(rig.writes.lock().unwrap().frames.len(), 3);
    assert_eq!(
        rig.writes.lock().unwrap().frames.last(),
        Some(&EncodedFrame::black())
    );
    let WorkerEvent::Ring {
        report:
            RingFeedback::Blanked {
                reason: RingBlankReason::ControlLost,
                write_finished_at_us,
                ..
            },
    } = rig.event()
    else {
        panic!("missing cleanup receipt")
    };
    assert!(write_finished_at_us < allowed.snapshot.expires_at().as_micros());
    assert!(matches!(rig.event(), WorkerEvent::Fault { .. }));
}

#[test]
fn genuinely_older_expired_control_cannot_clear_a_newer_valid_cue() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let old = controller.snapshot(now()).unwrap();
    let mut wire = serde_json::to_value(old).unwrap();
    wire["expires_at"] = (old.issued_at().as_micros() + 1).into();
    let old_expired = serde_json::from_value(wire).unwrap();
    let current = frame(&mut controller, owner, 200_000);
    present(&rig, current);
    rig.control(Control::Authority {
        snapshot: old_expired,
    });
    rig.frame(current);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Presented { .. }
        }
    ));
    assert_eq!(rig.colored(), 2);
    assert_eq!(rig.writes.lock().unwrap().frames.len(), 3);
    rig.control(Control::Stop);
    rig.finish().unwrap();
}

#[test]
fn invalid_control_kind_or_malformed_frame_blanks_and_exits() {
    for invalid_control in [true, false] {
        let mut rig = Rig::start(&[]);
        rig.ready();
        let (mut controller, owner) = conversation(rig.boot);
        present(&rig, frame(&mut controller, owner, 200_000));
        if invalid_control {
            rig.control(Control::StartCapture);
        } else {
            rig.parent
                .lock()
                .unwrap()
                .data
                .send(serde_json::json!({"phase":"off"}))
                .unwrap();
        }
        assert!(rig.finish().is_err());
        assert_eq!(rig.colored(), 1);
        assert_eq!(rig.writes.lock().unwrap().frames.len(), 3);
        assert_eq!(
            rig.writes.lock().unwrap().frames.last(),
            Some(&EncodedFrame::black())
        );
    }
}

#[test]
fn future_data_snapshot_never_installs_or_renews_authority() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    rig.frame(original);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Rejected {
                reason: RingRejection::ExpiredRequest,
                ..
            }
        }
    ));
    assert!(
        rig.finish()
            .unwrap_err()
            .to_string()
            .contains("heartbeat expired")
    );
    assert_eq!(rig.colored(), 0);
}

#[test]
fn invalid_frame_bounds_reject_without_a_write_and_speaking_requires_playback() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    present(&rig, original);
    let count = rig.writes.lock().unwrap().frames.len();
    let cases = [
        (
            RingFrame {
                ceiling: 121,
                ..original
            },
            RingRejection::InvalidCeiling,
        ),
        (
            RingFrame {
                requested_at_us: u64::MAX,
                ..original
            },
            RingRejection::FutureRequest,
        ),
        (
            RingFrame {
                phase: RingPhase::Speaking,
                ..original
            },
            RingRejection::PhaseMismatch,
        ),
    ];
    for (invalid, expected) in cases {
        rig.frame(invalid);
        assert!(
            matches!(rig.event(), WorkerEvent::Ring { report: RingFeedback::Rejected { reason, .. } } if reason == expected)
        );
    }
    assert_eq!(rig.writes.lock().unwrap().frames.len(), count);
    controller.input_ended(now(), owner).unwrap();
    let plan = controller
        .plan_output(now(), owner, OutputKind::Speech, None)
        .unwrap();
    let speech = controller.issue_output(now(), plan, 200_000).unwrap();
    controller.playback_started(now(), speech).unwrap();
    let speaking = frame(&mut controller, owner, 200_000);
    assert_eq!(speaking.phase, RingPhase::Speaking);
    rig.control(Control::Authority {
        snapshot: speaking.snapshot,
    });
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Blanked { .. }
        }
    ));
    rig.frame(speaking);
    assert!(matches!(
        rig.event(),
        WorkerEvent::Ring {
            report: RingFeedback::Presented {
                phase: RingPhase::Speaking,
                ..
            }
        }
    ));
    rig.control(Control::Stop);
    rig.finish().unwrap();
}

#[test]
fn malformed_control_blanks_and_reports_cleanup_failure_without_retry() {
    let mut rig = Rig::start(&[3]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    present(&rig, frame(&mut controller, owner, 200_000));
    rig.parent
        .lock()
        .unwrap()
        .control
        .send(serde_json::json!({"kind":"stop","extra":1}))
        .unwrap();
    let error = rig.finish().unwrap_err().to_string();
    assert!(error.contains("malformed runtime message"));
    assert!(error.contains("injected write 3"));
    assert_eq!(rig.writes.lock().unwrap().frames.len(), 3);
    assert!(matches!(rig.event(), WorkerEvent::Fault { code } if code.contains("cleanup")));
}

#[test]
fn primary_and_cleanup_write_errors_are_both_reported_once() {
    let mut rig = Rig::start(&[2, 3]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let original = frame(&mut controller, owner, 200_000);
    rig.control(Control::Authority {
        snapshot: original.snapshot,
    });
    rig.frame(original);
    let error = rig.finish().unwrap_err().to_string();
    assert!(error.contains("injected write 2"));
    assert!(error.contains("injected write 3"));
    assert_eq!(rig.writes.lock().unwrap().frames.len(), 3);
    assert!(matches!(rig.event(), WorkerEvent::Fault { .. }));
}

#[test]
fn startup_failure_never_reports_ready_and_stop_failure_is_visible() {
    let mut failed = Rig::start(&[1]);
    assert!(matches!(failed.event(), WorkerEvent::Fault { .. }));
    assert!(failed.finish().is_err());
    assert_eq!(failed.writes.lock().unwrap().frames.len(), 1);
    let mut stopped = Rig::start(&[2]);
    stopped.ready();
    stopped.control(Control::Stop);
    assert!(
        stopped
            .finish()
            .unwrap_err()
            .to_string()
            .contains("injected write 2")
    );
    assert_eq!(stopped.writes.lock().unwrap().frames.len(), 2);
}

#[test]
fn ring_wire_rejects_unknown_commands_and_unknown_nested_fields() {
    let (mut controller, owner) = conversation(new_boot().unwrap());
    let original = frame(&mut controller, owner, 200_000);
    let mut value = serde_json::to_value(original).unwrap();
    value["extra"] = true.into();
    assert!(decode::<RingFrame>(&encode(&value).unwrap()).is_err());
    value.as_object_mut().unwrap().remove("extra");
    value["phase"] = "off".into();
    assert!(decode::<RingFrame>(&encode(&value).unwrap()).is_err());
    value["phase"] = "listening".into();
    value["snapshot"]["extra"] = true.into();
    assert!(decode::<RingFrame>(&encode(&value).unwrap()).is_err());
    assert!(decode::<RingFeedback>(br#"{"kind":"blanked","reason":{"kind":"shutdown","extra":true},"write_started_at_us":1,"write_finished_at_us":2}"#).is_err());
}

#[test]
fn local_socket_request_to_fake_sink_latency_is_measured_without_physical_claim() {
    let mut rig = Rig::start(&[]);
    rig.ready();
    let (mut controller, owner) = conversation(rig.boot);
    let mut delays = Vec::new();
    for _ in 0..40 {
        let frame = frame(&mut controller, owner, 200_000);
        rig.control(Control::Authority {
            snapshot: frame.snapshot,
        });
        rig.frame(frame);
        let WorkerEvent::Ring {
            report:
                RingFeedback::Presented {
                    requested_at_us,
                    write_finished_at_us,
                    ..
                },
        } = rig.event()
        else {
            panic!("missing present receipt")
        };
        delays.push(write_finished_at_us - requested_at_us);
    }
    rig.control(Control::Stop);
    rig.finish().unwrap();
    delays.sort_unstable();
    let p95 = delays[(delays.len() * 95).div_ceil(100) - 1];
    println!(
        "{{\"scope\":\"local_unix_sockets_fake_sink_only\",\"samples\":{},\"p50_us\":{},\"p95_us\":{p95},\"max_us\":{},\"target_us\":50000,\"target_met\":{}}}",
        delays.len(),
        delays[19],
        delays[39],
        p95 <= 50_000
    );
}
