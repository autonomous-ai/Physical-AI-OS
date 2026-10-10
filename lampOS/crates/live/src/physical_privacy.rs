//! Dedicated GPIO process for the installed active-low privacy switch.
//! Linux ownership is exclusive; an existing HAL owner must be stopped before
//! opening this line. Unknown or lost control makes the qualification stop.
use crate::{
    transport::WorkerChannels,
    wire::{Control, WorkerEvent},
};
use gpiocdev::{
    Request,
    line::{Bias, Value},
};
use lamp_ipc::monotonic_us;
use std::{io, time::Duration};

pub fn run(mut channels: WorkerChannels) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let request = Request::builder()
        .on_chip("/dev/gpiochip1")
        .with_line(9)
        .with_consumer("lampOS-privacy")
        .as_input()
        .with_bias(Bias::PullUp)
        .request()?;
    channels.control.send(WorkerEvent::Ready)?;
    let mut last_control = monotonic_us();
    loop {
        for _ in 0..16 {
            match channels.control.receive()? {
                Some(Control::Stop) => return Ok(()),
                Some(Control::Authority { .. }) => last_control = monotonic_us(),
                Some(
                    Control::StartCapture
                    | Control::ConnectReference { .. }
                    | Control::ProviderOutputCapacity { .. },
                ) => {
                    return Err(io::Error::other("invalid privacy worker command").into());
                }
                None => break,
            }
        }
        if monotonic_us().saturating_sub(last_control) > 250_000 {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "privacy controller lease expired",
            )
            .into());
        }
        // No active-low flag was requested: Inactive is electrical low => mute.
        let muted = request.value(9)? == Value::Inactive;
        channels.control.send(WorkerEvent::Privacy {
            acquired_at_us: monotonic_us(),
            muted,
        })?;
        std::thread::sleep(Duration::from_millis(5));
    }
}
