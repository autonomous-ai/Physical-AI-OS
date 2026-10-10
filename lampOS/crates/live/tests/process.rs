use lamp_interaction::{Controller, MonoTime};
use lamp_ipc::monotonic_us;
use lamp_live::{
    process::{SessionDirectory, Worker, new_boot},
    wire::{Control, WorkerEvent},
};
use std::{
    path::Path,
    time::{Duration, Instant},
};

#[test]
fn real_worker_handshake_control_and_finite_shutdown() {
    let dir = SessionDirectory::create().unwrap();
    let boot = new_boot().unwrap();
    let mut worker = Worker::spawn(
        Path::new(env!("CARGO_BIN_EXE_lamp-live")),
        &dir.path,
        "probe",
        boot,
        &[],
    )
    .unwrap();
    let mut owner = Controller::new(boot, MonoTime::from_micros(monotonic_us()));
    worker
        .channels
        .control
        .send(Control::Authority {
            snapshot: owner
                .snapshot(MonoTime::from_micros(monotonic_us()))
                .unwrap(),
        })
        .unwrap();
    let deadline = Instant::now() + Duration::from_millis(100);
    loop {
        if let Some(WorkerEvent::Ready) = worker.channels.control.receive().unwrap() {
            break;
        }
        assert!(Instant::now() < deadline, "control receipt timed out");
        std::thread::sleep(Duration::from_millis(2));
    }
    worker.shutdown().unwrap();
}
