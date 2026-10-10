//! Finite read-only diagnostic, never a motion controller or daemon.

use lamp_motor::calibration::UnitId;
use std::{ffi::OsString, path::PathBuf};

const USAGE: &str = "lamp-motor-inspect --device ABSOLUTE_PATH --unit-id EXPLICIT_ID --lock-directory PRIVATE_ABSOLUTE_DIRECTORY\n\nRead-only Linux inspection: five pings and four five-servo register reads.\nNo register writes or torque/motion commands. Stop all foreign bus owners first.\nThe lock directory must already exist as 0700 under trusted non-writable ancestors.\nOpening an unqualified serial adapter may change modem lines. Hardware review is required.\n";

#[derive(Debug)]
struct Arguments {
    device: PathBuf,
    unit_id: UnitId,
    lock_directory: PathBuf,
}

fn parse(args: impl IntoIterator<Item = OsString>) -> Result<Option<Arguments>, &'static str> {
    let mut args = args.into_iter();
    let mut device = None;
    let mut unit_id = None;
    let mut lock_directory = None;
    while let Some(key) = args.next() {
        if key == "--help" {
            if device.is_some()
                || unit_id.is_some()
                || lock_directory.is_some()
                || args.next().is_some()
            {
                return Err("--help must be used alone");
            }
            return Ok(None);
        }
        let value = args.next().ok_or("every option requires a value")?;
        match key.to_str() {
            Some("--device") if device.is_none() => device = Some(PathBuf::from(value)),
            Some("--lock-directory") if lock_directory.is_none() => {
                lock_directory = Some(PathBuf::from(value))
            }
            Some("--unit-id") if unit_id.is_none() => {
                unit_id = Some(
                    UnitId::new(value.to_str().ok_or("unit ID must be UTF-8")?)
                        .map_err(|_| "invalid explicit unit ID")?,
                );
            }
            _ => return Err("unknown or duplicate option"),
        }
    }
    let result = Arguments {
        device: device.ok_or("--device is required")?,
        unit_id: unit_id.ok_or("--unit-id is required")?,
        lock_directory: lock_directory.ok_or("--lock-directory is required")?,
    };
    for path in [&result.device, &result.lock_directory] {
        if !path.is_absolute()
            || path.as_os_str().len() > 4096
            || path.to_str().is_none()
            || path.components().count() > 64
            || path
                .components()
                .any(|part| matches!(part, std::path::Component::ParentDir))
        {
            return Err(
                "device and lock directory must be bounded absolute UTF-8 paths without parent traversal",
            );
        }
    }
    Ok(Some(result))
}

fn main() -> std::process::ExitCode {
    let arguments = match parse(std::env::args_os().skip(1)) {
        Ok(None) => {
            print!("{USAGE}");
            return std::process::ExitCode::SUCCESS;
        }
        Ok(Some(arguments)) => arguments,
        Err(error) => {
            eprintln!("{error}\n{USAGE}");
            return std::process::ExitCode::from(2);
        }
    };
    match run(arguments) {
        Ok(true) => std::process::ExitCode::SUCCESS,
        Ok(false) => std::process::ExitCode::FAILURE,
        Err(error) => {
            eprintln!("motor inspection failed: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn run(arguments: Arguments) -> Result<bool, Box<dyn std::error::Error>> {
    // The host parser is testable; no device backend is compiled on this host.
    let _ = arguments.unit_id;
    Err("read-only motor inspection requires Linux; no device was opened".into())
}

#[cfg(target_os = "linux")]
fn run(arguments: Arguments) -> Result<bool, Box<dyn std::error::Error>> {
    use lamp_motor::linux::{InspectionConfig, inspect_once};
    use nix::sys::{
        signal::{SigSet, Signal},
        signalfd::{SfdFlags, SignalFd},
    };
    use std::io::Write;

    // This finite CLI is single-threaded. Mask restoration follows device close.
    struct SignalMask(SigSet);
    impl Drop for SignalMask {
        fn drop(&mut self) {
            let _ = self.0.thread_set_mask();
        }
    }
    let config = InspectionConfig::new(
        arguments.device,
        arguments.unit_id,
        arguments.lock_directory,
    )?;
    let old_mask = SigSet::thread_get_mask()?;
    let mut mask = SigSet::empty();
    mask.add(Signal::SIGINT);
    mask.add(Signal::SIGTERM);
    mask.thread_block()?;
    let restore = SignalMask(old_mask);
    let signal_fd = SignalFd::with_flags(&mask, SfdFlags::SFD_CLOEXEC | SfdFlags::SFD_NONBLOCK)?;
    let mut cancelled = false;
    let result = inspect_once(&config, || {
        if !cancelled {
            cancelled = signal_fd
                .read_signal()
                .map_err(|error| std::io::Error::from_raw_os_error(error as i32))?
                .is_some();
        }
        Ok(cancelled)
    });
    // Print only the bounded inspection report, after the serial descriptor is closed.
    let mut output = std::io::stdout().lock();
    serde_json::to_writer(&mut output, &result)?;
    writeln!(output)?;
    output.flush()?;
    restore.0.thread_set_mask()?;
    // A redundant set on Drop is safe; it owns no hardware and performs no wait.
    Ok(result.completed)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    #[test]
    fn no_implicit_device_or_unit_and_no_unknown_controls() {
        for values in [
            vec![],
            vec!["--device", "/dev/example"],
            vec![
                "--device",
                "/dev/example",
                "--unit-id",
                "lamp",
                "--lock-directory",
                "/home/test/private",
                "--torque",
                "on",
            ],
            vec![
                "--device",
                "/dev/a",
                "--device",
                "/dev/b",
                "--unit-id",
                "lamp",
                "--lock-directory",
                "/home/test/private",
            ],
        ] {
            assert!(parse(args(&values)).is_err());
        }
    }

    #[test]
    fn explicit_bounded_arguments_and_help_are_valid_without_any_open() {
        let value = parse(args(&[
            "--device",
            "/dev/example",
            "--unit-id",
            "lamp-test",
            "--lock-directory",
            "/home/test/private",
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(value.unit_id.as_str(), "lamp-test");
        assert_eq!(value.device, PathBuf::from("/dev/example"));
        assert!(parse(args(&["--help"])).unwrap().is_none());
        assert!(parse(args(&["--help", "--device", "/dev/a"])).is_err());
        assert!(
            parse(args(&[
                "--device",
                "relative",
                "--unit-id",
                "lamp-test",
                "--lock-directory",
                "/home/test/private"
            ]))
            .is_err()
        );
    }
}
