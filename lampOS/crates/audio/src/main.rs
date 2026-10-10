use lamp_audio::{
    Result,
    qualification::{benchmark, benchmark_fixture, export_fixture, write_report_new},
};
use std::{io, path::Path};

fn main() {
    if let Err(error) = run() {
        eprintln!("lamp-audio: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        #[cfg(unix)]
        Some("replay-aec") => {
            use lamp_audio::replay::{HistoricalProcessing, ReplayOptions, replay};
            let input = args
                .next()
                .ok_or_else(|| usage("missing diagnostic run directory"))?;
            let output = args
                .next()
                .ok_or_else(|| usage("missing fresh replay output directory"))?;
            let mut options = ReplayOptions::default();
            while let Some(flag) = args.next() {
                match flag.as_str() {
                    "--exploratory-prefix" if !options.exploratory_prefix => {
                        options.exploratory_prefix = true
                    }
                    "--historical-processing" if options.historical_processing.is_none() => {
                        let noise_suppression = match args.next().as_deref() {
                            Some("on") => true,
                            Some("off") => false,
                            _ => {
                                return Err(usage(
                                    "historical processing requires on|off SOURCE_EVIDENCE.json",
                                ));
                            }
                        };
                        let source_evidence = args
                            .next()
                            .ok_or_else(|| usage("missing historical source evidence file"))?;
                        options.historical_processing = Some(HistoricalProcessing {
                            noise_suppression,
                            source_evidence: source_evidence.into(),
                        });
                    }
                    _ => return Err(usage("unknown or repeated replay option")),
                }
            }
            let report = replay(
                &std::env::current_dir()?,
                Path::new(&input),
                Path::new(&output),
                options,
            )?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Some("bench-aec") => {
            let blocks = args
                .next()
                .map(|x| x.parse::<usize>())
                .transpose()?
                .unwrap_or(6000);
            no_extra(args)?;
            println!("{}", serde_json::to_string_pretty(&benchmark(blocks)?)?);
        }
        Some("export-aec-fixture") => {
            let destination = args
                .next()
                .ok_or_else(|| usage("missing new fixture directory"))?;
            let blocks = args
                .next()
                .map(|x| x.parse::<usize>())
                .transpose()?
                .unwrap_or(6000);
            no_extra(args)?;
            let manifest = export_fixture(destination, blocks)?;
            println!("{}", serde_json::to_string_pretty(&manifest)?);
        }
        Some("bench-aec-fixture") => {
            let fixture = args
                .next()
                .ok_or_else(|| usage("missing fixture directory"))?;
            let report_path = args
                .next()
                .ok_or_else(|| usage("missing new report path"))?;
            no_extra(args)?;
            // Fail early for existing reports; atomic publication also protects
            // against a report appearing after this preflight check.
            match std::fs::symlink_metadata(Path::new(&report_path)) {
                Ok(_) => return Err(usage("report destination already exists")),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
            let report = benchmark_fixture(fixture)?;
            write_report_new(report_path, &report)?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        _ => {
            return Err(usage(
                "usage: lamp-audio replay-aec RUN NEW_OUT [--exploratory-prefix] [--historical-processing on|off SOURCE_EVIDENCE.json] | bench-aec [500..60000 blocks] | export-aec-fixture NEW_DIR [blocks] | bench-aec-fixture FIXTURE_DIR NEW_REPORT.json",
            ));
        }
    }
    Ok(())
}

fn no_extra(mut args: impl Iterator<Item = String>) -> Result<()> {
    if args.next().is_some() {
        return Err(usage("extra arguments"));
    }
    Ok(())
}

fn usage(message: &'static str) -> Box<dyn std::error::Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidInput, message).into()
}
