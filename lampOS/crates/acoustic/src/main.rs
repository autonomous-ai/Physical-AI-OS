use lamp_acoustic::{Cache, MacSay, Result, catalog, invalid_argument, render, save_report};
use std::path::PathBuf;

fn main() {
    if let Err(error) = run() {
        eprintln!("lamp-acoustic: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("list") if args.next().is_none() => {
            println!("{}", serde_json::to_string_pretty(&catalog()?)?);
        }
        Some("render") => {
            let cache = PathBuf::from(
                args.next()
                    .ok_or_else(|| invalid_argument("missing cache directory"))?,
            );
            let manifest = PathBuf::from(
                args.next()
                    .ok_or_else(|| invalid_argument("missing output manifest"))?,
            );
            if args.next().is_some() || manifest.exists() {
                return Err(invalid_argument(
                    "extra arguments or an existing output manifest",
                ));
            }
            let cache = Cache::new(cache)?;
            let report = render(&catalog()?, &cache, &mut MacSay)?;
            save_report(&manifest, &report)?;
            println!(
                "{}",
                serde_json::json!({
                    "status": report.status,
                    "generated_speech": report.generated_speech,
                    "reused_speech": report.reused_speech,
                    "generated_scenes": report.generated_scenes,
                    "reused_scenes": report.reused_scenes,
                    "manifest": manifest,
                })
            );
        }
        Some("verify") => {
            let path = PathBuf::from(
                args.next()
                    .ok_or_else(|| invalid_argument("missing cache directory"))?,
            );
            if args.next().is_some() || !path.is_dir() {
                return Err(invalid_argument(
                    "extra arguments or missing cache directory",
                ));
            }
            let count = Cache::new(path)?.verify()?;
            println!("{}", serde_json::json!({"verified_objects": count}));
        }
        _ => {
            return Err(invalid_argument(
                "usage: lamp-acoustic list | render CACHE NEW_REPORT.json | verify CACHE",
            ));
        }
    }
    Ok(())
}
