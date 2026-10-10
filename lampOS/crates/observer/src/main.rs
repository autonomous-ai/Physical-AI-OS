use lamp_observer::{
    RecordOptions, Result,
    playback::{self, PlayOptions},
};
use std::{io, path::PathBuf};

fn main() {
    match run() {
        Ok(valid) => {
            if !valid {
                std::process::exit(2);
            }
        }
        Err(error) => {
            eprintln!("lamp-observer: {error}");
            std::process::exit(1);
        }
    }
}

fn usage() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "usage: lamp-observer devices | authorization [--out NEW_DIR] | authorize --out NEW_DIR | record --input EXACT_NAME --seconds 1..600 --out NEW_DIR | inspect-output --output 'iMac Speakers' | play --output 'iMac Speakers' --wav CACHED.wav --out NEW_DIR [--cancel-file PATH]",
    )
}

fn run() -> Result<bool> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("devices") if args.next().is_none() => {
            println!(
                "{}",
                serde_json::to_string_pretty(&lamp_observer::devices()?)?
            );
            Ok(true)
        }
        Some(command @ ("authorization" | "authorize")) => {
            let output = match args.next().as_deref() {
                None => None,
                Some("--out") => Some(PathBuf::from(args.next().ok_or_else(usage)?)),
                _ => return Err(usage().into()),
            };
            if args.next().is_some() {
                return Err(usage().into());
            }
            let report =
                lamp_observer::authorization::probe(command == "authorize", output.as_deref())?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            Ok(report["capture_allowed"] == true)
        }
        Some("inspect-output") => {
            if args.next().as_deref() != Some("--output") {
                return Err(usage().into());
            }
            let name = args.next().ok_or_else(usage)?;
            if args.next().is_some() {
                return Err(usage().into());
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&playback::inspect_output(&name)?)?
            );
            Ok(true)
        }
        Some("play") => {
            let options = parse_play(args)?;
            let result = playback::play(&options)?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(result["valid"] == true)
        }
        Some("record") => {
            let mut input = None;
            let mut seconds = None;
            let mut output = None;
            while let Some(option) = args.next() {
                let value = args.next().ok_or_else(usage)?;
                match option.as_str() {
                    "--input" if input.is_none() => input = Some(value),
                    "--seconds" if seconds.is_none() => seconds = Some(value.parse::<u16>()?),
                    "--out" if output.is_none() => output = Some(PathBuf::from(value)),
                    _ => return Err(usage().into()),
                }
            }
            let options = RecordOptions {
                input: input.ok_or_else(usage)?,
                seconds: seconds.ok_or_else(usage)?,
                output: output.ok_or_else(usage)?,
            };
            let result = lamp_observer::record(&options)?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(result["valid"] == true)
        }
        _ => Err(usage().into()),
    }
}

fn parse_play(mut args: impl Iterator<Item = String>) -> Result<PlayOptions> {
    let (mut device, mut wav, mut output, mut cancel_file) = (None, None, None, None);
    while let Some(option) = args.next() {
        let value = args.next().ok_or_else(usage)?;
        match option.as_str() {
            "--output" if device.is_none() => device = Some(value),
            "--wav" if wav.is_none() => wav = Some(PathBuf::from(value)),
            "--out" if output.is_none() => output = Some(PathBuf::from(value)),
            "--cancel-file" if cancel_file.is_none() => cancel_file = Some(PathBuf::from(value)),
            _ => return Err(usage().into()),
        }
    }
    let options = PlayOptions {
        output_device: device.ok_or_else(usage)?,
        wav: wav.ok_or_else(usage)?,
        output: output.ok_or_else(usage)?,
        cancel_file,
    };
    options.validate()?;
    Ok(options)
}

#[cfg(test)]
mod playback_cli_tests {
    use super::*;
    fn parse(args: &[&str]) -> Result<PlayOptions> {
        parse_play(args.iter().map(|s| (*s).to_string()))
    }
    #[test]
    fn explicit_route_fixture_and_fresh_report_are_required() {
        let good = [
            "--output",
            "iMac Speakers",
            "--wav",
            "cached.wav",
            "--out",
            "new",
        ];
        assert!(parse(&good).is_ok());
        for i in (0..good.len()).step_by(2) {
            let mut missing = good.to_vec();
            missing.drain(i..i + 2);
            assert!(parse(&missing).is_err());
        }
        let mut duplicate = good.to_vec();
        duplicate.extend_from_slice(&["--wav", "other.wav"]);
        assert!(parse(&duplicate).is_err());
        let mut default = good;
        default[1] = "default";
        assert!(parse(&default).is_err());
        let mut cancelled = good.to_vec();
        cancelled.extend_from_slice(&["--cancel-file", "cancel"]);
        assert_eq!(
            parse(&cancelled).unwrap().cancel_file,
            Some(PathBuf::from("cancel"))
        );
        cancelled.push("--unknown");
        assert!(parse(&cancelled).is_err());
    }
}
