//! Process supervision is compiled on Unix hosts for synthetic tests. Only the
//! Linux CLI exposes entry; the only physical workers are GPIO privacy + camera.
use super::*;
use crate::{
    privacy::{PRIVACY_FRESH_US, PrivacyGate},
    process::{SessionDirectory, Worker, new_boot},
    wire::{Control, WorkerEvent},
};
use lamp_interaction::{Controller, MonoTime, Permission};
use lamp_ipc::monotonic_us;
use std::{
    fs::OpenOptions, io::Write, os::unix::fs::OpenOptionsExt, path::Path, process::Command,
    time::Duration,
};

const PUBLISH_US: u64 = 20_000;
const PERMISSION_WAIT_US: u64 = 3_000_000;
const PRIVACY_STOP_US: u64 = 100_000;

struct Authority {
    controller: Controller,
    privacy: PrivacyGate,
    sequence: u64,
    last_acquired: Option<u64>,
    last_publish: Option<u64>,
    started: bool,
    last_observation: Option<PrivacyObservation>,
    revoke_at_us: Option<u64>,
}
impl Authority {
    fn new(boot: BootId, now_us: u64) -> Self {
        Self {
            controller: Controller::new(boot, t(now_us)),
            privacy: PrivacyGate::default(),
            sequence: 0,
            last_acquired: None,
            last_publish: None,
            started: false,
            last_observation: None,
            revoke_at_us: None,
        }
    }
    /// Return false on budget exhaustion, so metadata remains retained rather
    /// than being accepted while newer physical privacy data might be waiting.
    fn drain(&mut self, worker: &mut Worker, remaining: &mut usize) -> io::Result<bool> {
        while *remaining > 0 {
            let Some(event) = worker.channels.control.receive()? else {
                return Ok(true);
            };
            *remaining -= 1;
            match event {
                WorkerEvent::Privacy {
                    acquired_at_us,
                    muted,
                } => {
                    let now_us = monotonic_us();
                    if now_us
                        .checked_sub(acquired_at_us)
                        .is_none_or(|age| age >= PRIVACY_FRESH_US)
                        || self.last_acquired.is_some_and(|last| acquired_at_us < last)
                    {
                        return Err(io::Error::other(
                            "invalid or stale physical privacy observation",
                        ));
                    }
                    self.sequence = self
                        .sequence
                        .checked_add(1)
                        .ok_or_else(|| io::Error::other("privacy sequence overflow"))?;
                    self.last_acquired = Some(acquired_at_us);
                    self.last_observation = Some(PrivacyObservation {
                        sequence: self.sequence,
                        acquired_at_us,
                        received_at_us: now_us,
                        muted,
                    });
                    self.privacy
                        .observe(self.sequence, t(acquired_at_us), t(now_us), muted);
                    self.update_permission(now_us)?;
                }
                _ => {
                    return Err(io::Error::other(
                        "unexpected physical privacy worker message",
                    ));
                }
            }
        }
        Ok(false)
    }
    fn update_permission(&mut self, now_us: u64) -> io::Result<bool> {
        let allowed = self.privacy.allowed(t(now_us));
        self.controller
            .set_camera_permission(
                t(now_us),
                if allowed {
                    Permission::Allowed
                } else {
                    Permission::Denied
                },
            )
            .map_err(io::Error::other)?;
        if self.started && !allowed {
            self.revoke_at_us.get_or_insert(now_us);
            return Err(io::Error::other(
                "physical camera privacy revoked or observation expired",
            ));
        }
        Ok(allowed)
    }
    fn publish(
        &mut self,
        privacy: &mut Worker,
        camera: Option<&mut Worker>,
        force: bool,
    ) -> io::Result<()> {
        let now_us = monotonic_us();
        self.update_permission(now_us)?;
        if force
            || self
                .last_publish
                .is_none_or(|at| now_us.saturating_sub(at) >= PUBLISH_US)
        {
            let snapshot = self
                .controller
                .snapshot(t(now_us))
                .map_err(io::Error::other)?;
            // Camera control is always enqueued before a StartCapture on that
            // same FIFO. Privacy and camera have distinct bounded channels.
            if let Some(camera) = camera {
                camera
                    .channels
                    .control
                    .send(Control::Authority { snapshot })?;
            }
            privacy
                .channels
                .control
                .send(Control::Authority { snapshot })?;
            self.last_publish = Some(now_us);
        }
        Ok(())
    }
    fn grant(&mut self, now_us: u64) -> io::Result<Option<CameraGrant>> {
        if self.update_permission(now_us)? {
            Ok(Some(
                self.controller
                    .camera_grant(t(now_us))
                    .map_err(io::Error::other)?,
            ))
        } else {
            Ok(None)
        }
    }
}
fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}

pub fn run(config_path: &Path, seconds: u64, output_path: &Path) -> io::Result<()> {
    let (config, digest) = CameraConfig::load(config_path, None)?;
    let mut report = MetadataReport::new(config, digest.clone(), seconds, monotonic_us())?;
    if !crate::camera_config::valid_absolute(output_path) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "camera report path must be absolute and new",
        ));
    }
    let flags = rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC;
    // Reserve the private output before hardware use; actual report I/O happens
    // only after both workers have been stopped/reaped.
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(flags.bits() as i32)
        .open(output_path)?;
    let directory = SessionDirectory::create()?;
    let boot = new_boot()?;
    let mut authority = Authority::new(boot, monotonic_us());
    let executable = std::env::current_exe()?;
    let mut privacy: Option<Worker> = None;
    let mut camera: Option<Worker> = None;
    let result = (|| -> io::Result<()> {
        refuse_legacy_owners()?;
        privacy = Some(Worker::spawn(
            &executable,
            &directory.path,
            "privacy",
            boot,
            &[],
        )?);
        let privacy_worker = privacy.as_mut().expect("privacy spawned");
        authority.publish(privacy_worker, None, true)?;
        let path = config_path
            .to_str()
            .ok_or_else(|| io::Error::other("camera config path must be UTF-8"))?;
        camera = Some(Worker::spawn_with_tick(
            &executable,
            &directory.path,
            "camera",
            boot,
            &[path, &digest],
            || {
                privacy_worker.check_running()?;
                authority.drain(privacy_worker, &mut 16)?;
                authority.publish(privacy_worker, None, false)
            },
        )?);
        let camera_worker = camera.as_mut().expect("camera spawned closed");
        authority.publish(privacy_worker, Some(camera_worker), true)?;
        inspect_loop(&mut authority, privacy_worker, camera_worker, &mut report)
    })();
    if let Err(error) = &result {
        report.error = Some(error.to_string());
    }
    report.last_physical_privacy = authority.last_observation;
    report.parent_privacy_revoke_at_us = authority.revoke_at_us;
    let shutdown = shutdown_pair(
        &mut authority.controller,
        privacy.as_mut(),
        camera.as_mut(),
        &mut report,
    );
    let shutdown_failed = !shutdown.errors.is_empty();
    report.shutdown = Some(shutdown);
    report.ended_at_us = Some(monotonic_us());
    report.outcome = if result.is_ok() && !shutdown_failed {
        "completed_metadata_only"
    } else {
        "failed"
    }
    .into();
    if shutdown_failed && report.error.is_none() {
        report.error = Some("worker shutdown or cleanup failed; see shutdown evidence".into());
    }
    serde_json::to_writer_pretty(&mut output, &report).map_err(io::Error::other)?;
    output.write_all(b"\n")?;
    output.flush()?;
    result?;
    if shutdown_failed {
        return Err(io::Error::other("camera inspection shutdown failed"));
    }
    Ok(())
}

fn inspect_loop(
    authority: &mut Authority,
    privacy: &mut Worker,
    camera: &mut Worker,
    report: &mut MetadataReport,
) -> io::Result<()> {
    let initial = monotonic_us();
    let permission_deadline = initial
        .checked_add(PERMISSION_WAIT_US)
        .ok_or_else(|| io::Error::other("camera permission deadline overflow"))?;
    let mut end_at = None;
    let mut observer = Observer::new(camera.boot, initial);
    let mut pending = None;
    loop {
        let mut remaining = 16;
        let control_empty = authority.drain(privacy, &mut remaining)?;
        privacy.check_running()?;
        camera.check_running()?;
        let now_us = monotonic_us();
        let allowed = authority.update_permission(now_us)?;
        if !authority.started && control_empty && allowed {
            authority.publish(privacy, Some(camera), true)?;
            camera.channels.control.send(Control::StartCapture)?;
            authority.started = true;
            end_at = Some(
                now_us
                    .checked_add(report.requested_seconds * 1_000_000)
                    .ok_or_else(|| io::Error::other("inspection deadline overflow"))?,
            );
        } else if !authority.started && now_us >= permission_deadline {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "no stable physical camera permission within three seconds",
            ));
        }
        authority.publish(privacy, Some(camera), false)?;
        if control_empty {
            accept_one(
                authority,
                privacy,
                &mut observer,
                report,
                &mut pending,
                &mut remaining,
                || camera.channels.data.receive::<CameraEvent>(),
            )?;
        }
        let now_us = monotonic_us();
        observer.check_deadline(now_us)?;
        if end_at.is_some_and(|deadline| now_us >= deadline) {
            if report.frames_accepted == 0 {
                return Err(io::Error::other("inspection ended without a current frame"));
            }
            return Ok(());
        }
        std::thread::sleep(Duration::from_micros(lamp_camera::POLL_TARGET_US));
    }
}

fn accept_one(
    authority: &mut Authority,
    privacy: &mut Worker,
    observer: &mut Observer,
    report: &mut MetadataReport,
    pending: &mut Option<CameraEvent>,
    remaining: &mut usize,
    mut receive: impl FnMut() -> io::Result<Option<CameraEvent>>,
) -> io::Result<()> {
    if pending.is_none() {
        *pending = receive()?;
    }
    // A physical switch event can arrive after the first empty control read,
    // even though data was already queued. Check again before accepting it.
    if authority.drain(privacy, remaining)?
        && let Some(event) = pending.take()
    {
        let at_us = monotonic_us();
        let grant = authority.grant(at_us)?;
        observer.accept(event, grant, at_us, report)?;
    }
    Ok(())
}

fn refuse_legacy_owners() -> io::Result<()> {
    // Temporary coexistence guard, not a dependency on a legacy service/API.
    // This check cannot exclude an unrelated application ignoring camera locks.
    for unit in [
        "hal.service",
        "os-server.service",
        "led-boot.service",
        "led-shutdown.service",
    ] {
        let status = Command::new("/usr/bin/systemctl")
            .args(["is-active", "--quiet", unit])
            .status()?;
        if status.success() {
            return Err(io::Error::other(format!(
                "{unit} still active; explicit exclusive cutover required"
            )));
        }
        if !matches!(status.code(), Some(3) | Some(4)) {
            return Err(io::Error::other(
                "cannot verify competing legacy service state",
            ));
        }
    }
    Ok(())
}

fn shutdown_pair(
    controller: &mut Controller,
    mut privacy: Option<&mut Worker>,
    mut camera: Option<&mut Worker>,
    report: &mut MetadataReport,
) -> ShutdownReport {
    let requested_at_us = monotonic_us();
    let mut result = ShutdownReport {
        requested_at_us,
        ..ShutdownReport::default()
    };
    let deny = controller
        .set_camera_permission(t(requested_at_us), Permission::Denied)
        .and_then(|()| controller.snapshot(t(requested_at_us)));
    if let Err(error) = deny {
        result.record_error(format!("camera deny authority: {error}"));
    }
    if let Some(camera) = camera.as_deref_mut() {
        if let Ok(snapshot) = deny
            && let Err(error) = camera
                .channels
                .control
                .send(Control::Authority { snapshot })
        {
            result.record_error(format!("camera deny send: {error}"));
        }
        if let Err(error) = camera.channels.control.send(Control::Stop) {
            result.record_error(format!("camera stop send: {error}"));
        }
    }
    if let Some(privacy) = privacy.as_deref_mut()
        && let Err(error) = privacy.channels.control.send(Control::Stop)
    {
        result.record_error(format!("privacy stop send: {error}"));
    }
    let camera_deadline = requested_at_us.saturating_add(lamp_camera::REVOKE_TARGET_US);
    let privacy_deadline = requested_at_us.saturating_add(PRIVACY_STOP_US);
    while camera.is_some() || privacy.is_some() {
        if let Some(worker) = camera.as_deref_mut() {
            drain_shutdown_metadata(worker, report, &mut result);
            match worker.try_exit() {
                Ok(Some(status)) => {
                    // Recheck after observing exit: Stopped may have been sent
                    // between the earlier empty receive and try_wait.
                    drain_shutdown_metadata(worker, report, &mut result);
                    result.camera_exit_at_us = Some(monotonic_us());
                    if let Err(error) = worker.exit_result(status) {
                        result.record_error(error.to_string());
                    }
                    if report.final_worker_event.is_none() {
                        result.record_error(
                            "camera exited without observable stop/cleanup report".into(),
                        );
                    }
                    camera = None;
                }
                Ok(None) if monotonic_us() < camera_deadline => {}
                state => {
                    let error = worker.terminate_and_reap(
                        io::ErrorKind::TimedOut,
                        format!("camera stop not completed within 20 ms; state={state:?}"),
                    );
                    result.camera_forced = true;
                    result.camera_exit_at_us = Some(monotonic_us());
                    result.record_error(error.to_string());
                    camera = None;
                }
            }
        }
        if let Some(worker) = privacy.as_deref_mut() {
            match worker.try_exit() {
                Ok(Some(status)) => {
                    result.privacy_exit_at_us = Some(monotonic_us());
                    if let Err(error) = worker.exit_result(status) {
                        result.record_error(error.to_string());
                    }
                    privacy = None;
                }
                Ok(None) if monotonic_us() < privacy_deadline => {}
                state => {
                    let error = worker.terminate_and_reap(
                        io::ErrorKind::TimedOut,
                        format!("privacy stop deadline expired; state={state:?}"),
                    );
                    result.privacy_forced = true;
                    result.privacy_exit_at_us = Some(monotonic_us());
                    result.record_error(error.to_string());
                    privacy = None;
                }
            }
        }
        if camera.is_some() || privacy.is_some() {
            std::thread::sleep(Duration::from_micros(lamp_camera::POLL_TARGET_US));
        }
    }
    result
}

fn drain_shutdown_metadata(
    worker: &mut Worker,
    report: &mut MetadataReport,
    result: &mut ShutdownReport,
) {
    for _ in 0..16 {
        match worker.channels.data.receive::<CameraEvent>() {
            Ok(Some(event)) => {
                if let Err(error) = retain_stop(event, report) {
                    result.record_error(error.to_string());
                }
            }
            Ok(None) => break,
            Err(error) => {
                result.record_error(format!("camera shutdown metadata: {error}"));
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::camera_worker::{Counts, FrameMetadata, IdentityMetadata, PortMetadata, Timing};
    fn config() -> CameraConfig {
        serde_json::from_value(serde_json::json!({
        "device":"/dev/nonexistent-explicit-camera","lock_directory":"/run/camera-test-private","source":"desk",
        "expected_usb":{"vendor":4660,"product":22136,"topology":"1-2","serial":null,"interface_number":0,"capture_index":0},
        "width":640,"height":480,"interval":null,"max_frame_bytes":256,"buffers":2})).unwrap()
    }
    #[test]
    fn physical_revoke_between_empty_control_and_metadata_receive_wins() {
        let directory = SessionDirectory::create().unwrap();
        let (mut privacy, mut sender) = Worker::sleeping_fixture(&directory.path, "privacy");
        let now_us = monotonic_us();
        let boot = new_boot().unwrap();
        let camera_boot = new_boot().unwrap();
        let mut authority = Authority::new(boot, now_us - 60_000);
        // Install a deterministic stable history, followed by real local IPC.

        for sequence in 1..=7 {
            let at = now_us - 60_000 + (sequence - 1) * 10_000;
            authority.privacy.observe(sequence, t(at), t(at), false);
        }
        authority.sequence = 7;
        authority.last_acquired = Some(now_us);
        authority.update_permission(now_us).unwrap();
        let grant = authority.grant(now_us).unwrap().unwrap();
        authority.started = true;
        let mut report = MetadataReport::new(config(), "a".repeat(64), 1, now_us).unwrap();
        let mut observer = Observer::new(camera_boot, now_us);
        let mode = Mode {
            source: "desk".into(),
            format: *b"MJPG",
            width: 640,
            height: 480,
            interval: None,
            size_image: 256,
            buffers: 2,
        };
        observer
            .accept(
                CameraEvent::Opening {
                    at_us: now_us,
                    deadline_us: now_us + 100_000,
                },
                Some(grant),
                now_us,
                &mut report,
            )
            .unwrap();
        observer
            .accept(
                CameraEvent::Started {
                    at_us: now_us,
                    worker: camera_boot,
                    capture_epoch: 1,
                    mode: mode.clone(),
                    timing: Timing {
                        started_at_us: now_us,
                        completed_at_us: now_us,
                    },
                    port: PortMetadata {
                        identity: Some(IdentityMetadata {
                            canonical_node: "/dev/test".into(),
                            major: 81,
                            minor: 0,
                            inode: 1,
                            usb: config().expected_usb,
                        }),
                        ..PortMetadata::default()
                    },
                },
                Some(grant),
                now_us,
                &mut report,
            )
            .unwrap();
        let mut event = Some(CameraEvent::Frame {
            frame: FrameMetadata {
                worker: camera_boot,
                capture_epoch: 1,
                sequence: 1,
                grant,
                mode,
                bytes_used: 4,
                driver_sequence: 1,
                driver_timestamp: None,
                dequeue: Timing {
                    started_at_us: now_us,
                    completed_at_us: now_us,
                },
                published_at_us: now_us,
            },
        });
        let mut budget = 16;
        assert!(authority.drain(&mut privacy, &mut budget).unwrap());
        let mut pending = None;
        let result = accept_one(
            &mut authority,
            &mut privacy,
            &mut observer,
            &mut report,
            &mut pending,
            &mut budget,
            || {
                sender.control.send(WorkerEvent::Privacy {
                    acquired_at_us: monotonic_us(),
                    muted: true,
                })?;
                Ok(event.take())
            },
        );
        assert!(result.unwrap_err().to_string().contains("privacy revoked"));
        assert_eq!(report.frames_accepted, 0);
        assert!(authority.last_observation.unwrap().muted);
        assert!(authority.revoke_at_us.is_some());
        let _ = privacy.terminate_and_reap(io::ErrorKind::Other, "synthetic test cleanup".into());
    }
    #[test]
    fn hung_camera_and_privacy_are_both_reaped_and_reported_as_failure() {
        let directory = SessionDirectory::create().unwrap();
        let (mut camera, _camera_peer) = Worker::sleeping_fixture(&directory.path, "camera");
        let (mut privacy, _privacy_peer) = Worker::sleeping_fixture(&directory.path, "privacy");
        let now_us = monotonic_us();
        let mut controller = Controller::new(new_boot().unwrap(), t(now_us));
        let mut report = MetadataReport::new(config(), "a".repeat(64), 1, now_us).unwrap();
        let shutdown = shutdown_pair(
            &mut controller,
            Some(&mut privacy),
            Some(&mut camera),
            &mut report,
        );
        assert!(shutdown.camera_forced && shutdown.privacy_forced);
        assert!(shutdown.errors.len() >= 2);
        assert!(camera.try_exit().unwrap().is_some() && privacy.try_exit().unwrap().is_some());
        assert_eq!(report.frames_accepted, 0);
    }
    #[test]
    fn post_revoke_frames_are_never_added_to_the_report() {
        let now_us = monotonic_us();
        let mut report = MetadataReport::new(config(), "a".repeat(64), 1, now_us).unwrap();
        retain_stop(
            CameraEvent::Progress {
                at_us: now_us,
                counts: Counts::default(),
                last_read: None,
            },
            &mut report,
        )
        .unwrap();
        assert!(report.final_worker_event.is_none());
        assert_eq!(report.frames_accepted, 0);
        retain_stop(
            CameraEvent::Stopped {
                at_us: now_us,
                reason: StopReason::Requested,
                counts: Counts::default(),
                stop: None,
                port: PortMetadata::default(),
                error: None,
            },
            &mut report,
        )
        .unwrap();
        assert!(report.final_worker_event.is_some());
    }
}
