use super::*;
use crate::{camera_config::CameraConfig, wire::WorkerEvent};
use lamp_camera::linux::{ControlReading, ControlState, IntervalStatus, LinuxPort, SystemClock};
use serde_json::json;
use std::{path::Path, time::Duration};

impl InspectPort for LinuxPort {
    fn metadata(&self) -> PortMetadata {
        let d = self.diagnostics();
        PortMetadata {
            identity: d.identity.as_ref().map(|i| IdentityMetadata {
                canonical_node: i.canonical_node.to_string_lossy().into_owned(), major: i.major,
                minor: i.minor, inode: i.inode,
                usb: UsbIdentity { vendor: i.usb.vendor, product: i.usb.product, topology: i.usb.topology.clone(),
                    serial: i.usb.serial.clone(), interface_number: i.usb.interface_number, capture_index: i.usb.capture_index },
            }),
            capabilities: d.capabilities.as_ref().map(|c| json!({ "driver":c.driver,"card":c.card,"bus_info":c.bus_info,
                "version":c.version,"capabilities":c.capabilities,"device_capabilities":c.device_capabilities })),
            interval_query: d.interval.map(|interval| match interval {
                IntervalStatus::Unsupported => json!({"status":"unsupported"}),
                IntervalStatus::Reported { can_set_interval, actual } => json!({"status":"reported",
                    "can_set_interval":can_set_interval,"actual":actual.map(Interval::from)}),
            }),
            controls: d.controls.as_ref().map(|controls| std::array::from_fn(|index| {
                let control = &controls[index];
                match &control.state {
                    ControlState::Unsupported => json!({"id":control.id,"status":"unsupported"}),
                    ControlState::Supported { name, kind, flags, minimum, maximum, step, default, reading } => {
                        let reading = match reading {
                            ControlReading::Value(value) => json!({"status":"value","value":value}),
                            ControlReading::Disabled => json!({"status":"disabled"}),
                            ControlReading::UnsupportedType => json!({"status":"unsupported_type"}),
                            ControlReading::Unavailable(errno) => json!({"status":"unavailable","errno":errno}),
                        };
                        json!({"id":control.id,"status":"supported","name":name,"type":kind,"flags":flags,
                            "minimum":minimum,"maximum":maximum,"step":step,"default":default,"reading":reading})
                    }
                }
            })),
            buffer_lengths: d.buffer_lengths, last_raw_buffer_flags: d.last_raw_buffer_flags,
            stop: d.stop.map(Into::into), fault: d.fault.map(|f| format!("{f:?}")),
            cleanup: d.cleanup.map(|c| format!("{c:?}")),
        }
    }
}

pub fn run(
    mut channels: WorkerChannels,
    parent: BootId,
    worker: BootId,
    path: &Path,
    digest: &str,
) -> io::Result<()> {
    let (config, _) = CameraConfig::load(path, Some(digest))?;
    let port = LinuxPort::new(config.linux()?);
    let capture = Capture::new(port, SystemClock, parent, worker, config.capture()?)
        .map_err(io::Error::other)?;
    let mut clock = lamp_ipc::monotonic_us;
    let mut runtime = CameraLoop::new(capture, clock());
    // No camera open or permission was obtained to reach this handshake.
    channels.control.send(WorkerEvent::Ready)?;
    loop {
        if runtime.step(&mut channels, &mut clock)? == Step::Stopped {
            return Ok(());
        }
        // This is a cooperative target, not a scheduler/kernel guarantee.
        std::thread::sleep(Duration::from_micros(lamp_camera::POLL_TARGET_US));
    }
}
