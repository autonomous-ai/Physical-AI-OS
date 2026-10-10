use lamp_live::{config::ProviderConfig, process::parse_boot, transport::WorkerChannels};
use std::{io, path::Path};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

fn main() {
    if let Err(error) = run() {
        eprintln!("lamp-live: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match args.as_slice() {
        [command,path] if command=="provider-check" => {
            let config=ProviderConfig::load(Path::new(path))?.into_session()?;
            tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {
                let started=lamp_ipc::monotonic_us();
                let connection=lamp_gemini::connect(config,lamp_gemini::SessionId::new(1)?).await?;
                println!("{}",serde_json::json!({"status":"setup_complete","setup_us":lamp_ipc::monotonic_us()-started,"microphone_opened":false,"audio_sent":false}));
                connection.shutdown();
                Ok::<(),Box<dyn std::error::Error+Send+Sync>>(())
            })?;
        },
        #[cfg(target_os = "linux")]
        [command, config, seconds, output] if command == "camera-inspect" => {
            lamp_live::camera_inspect::run(Path::new(config), seconds.parse()?, Path::new(output))?;
        }
        #[cfg(target_os = "linux")]
        [command, role, dir, parent, worker, config, digest] if command == "worker" && role == "camera" => {
            let parent = parse_boot(parent)?;
            let worker = parse_boot(worker)?;
            let mut channels = WorkerChannels::bind(Path::new(dir), role, parent, worker, true)?;
            channels.connect(Path::new(dir), role, true)?;
            lamp_live::camera_worker::run(channels, parent, worker, Path::new(config), digest)?;
        }
        #[cfg(target_os = "linux")]
        [command, wav, sha256, seconds, output, flags @ ..] if command == "directed-fixture" => {
            let options = lamp_live::fixture_provider::FixtureOptions::parse(flags)?;
            lamp_live::coordinator::run_fixture(Path::new(wav), sha256, seconds.parse()?, Path::new(output), options)?;
        }
        [command, role, dir, parent, worker, flag, wav, sha256]
            if command == "worker" && role == "provider" && flag == "--fixture" => {
            let parent = parse_boot(parent)?;
            let worker = parse_boot(worker)?;
            let mut channels = WorkerChannels::bind(Path::new(dir), role, parent, worker, true)?;
            channels.connect(Path::new(dir), role, true)?;
            lamp_live::fixture_provider::run(channels, parent, Path::new(wav), sha256)?;
        }
        #[cfg(target_os = "linux")]
        [command, config, seconds, output, flags @ ..] if command == "directed" => {
            let options = lamp_live::options::DirectedOptions::parse(flags)?;
            lamp_live::coordinator::run_directed_with_options(Path::new(config), seconds.parse()?, Path::new(output), options)?;
        }
        #[cfg(target_os = "linux")]
        [command, role, dir, parent, worker, device, flags @ ..]
            if command == "worker" && matches!(role.as_str(), "capture" | "speaker") => {
            let options = lamp_live::options::AudioWorkerOptions::parse(flags, role == "capture")?;
            let parent = parse_boot(parent)?;
            let worker = parse_boot(worker)?;
            let mut channels = WorkerChannels::bind(Path::new(dir), role, parent, worker, true)?;
            channels.connect(Path::new(dir), role, true)?;
            if role == "capture" {
                lamp_live::audio_workers::capture_with_noise_suppression(channels,parent,worker,Path::new(dir),device,options.diagnostic_directory.as_deref(),options.noise_suppression)?;
            } else {
                lamp_live::audio_workers::speaker(channels,parent,worker,Path::new(dir),device,options.diagnostic_directory.as_deref())?;
            }
        }
        [command, role, dir, parent, worker, extra] if command == "worker" => {
            let parent = parse_boot(parent)?;
            let worker = parse_boot(worker)?;
            let mut channels = WorkerChannels::bind(Path::new(dir), role, parent, worker, true)?;
            channels.connect(Path::new(dir), role, true)?;
            match role.as_str() {
                "provider" => tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(lamp_live::provider_worker::run(channels,parent,Path::new(extra)))?,
                _ => return Err(io::Error::other("unknown worker role").into()),
            }
        }
        [command,role,dir,parent,worker] if command=="worker" => {
            let parent=parse_boot(parent)?;
            let worker=parse_boot(worker)?;
            let mut channels=WorkerChannels::bind(Path::new(dir),role,parent,worker,true)?;
            channels.connect(Path::new(dir),role,true)?;
            match role.as_str() {
                #[cfg(target_os="linux")]
                "privacy"=>lamp_live::physical_privacy::run(channels)?,
                "probe"=>probe(channels)?,
                _=>return Err(io::Error::other("worker is not integrated yet").into()),
            }
        },
        _=>return Err(io::Error::new(io::ErrorKind::InvalidInput,"usage: lamp-live camera-inspect PRIVATE_CAMERA_CONFIG.json SECONDS NEW_REPORT.json | provider-check PRIVATE_CONFIG.json | directed PRIVATE_CONFIG.json SECONDS NEW_OUTPUT [--diagnostics] [--noise-suppression on|off] | directed-fixture REPLY.wav SHA256 SECONDS NEW_OUTPUT --diagnostics [--noise-suppression on|off] [--cue-socket ABS_PATH] | worker ROLE DIR CONTROLLER_BOOT WORKER_BOOT").into()),
    }
    Ok(())
}

fn probe(mut channels: WorkerChannels) -> Result<()> {
    use lamp_live::wire::{Control, WorkerEvent};
    channels.control.send(WorkerEvent::Ready)?;
    let mut last = lamp_ipc::monotonic_us();
    loop {
        match channels.control.receive()? {
            Some(Control::Stop) => return Ok(()),
            Some(Control::Authority { .. }) => {
                last = lamp_ipc::monotonic_us();
                channels.control.send(WorkerEvent::Ready)?;
            }
            Some(Control::StartCapture | Control::ConnectReference { .. }) => {
                return Err(io::Error::other("invalid probe command").into());
            }
            None => {}
        }
        if lamp_ipc::monotonic_us().saturating_sub(last) > 250_000 {
            return Err(io::Error::other("controller heartbeat expired").into());
        }
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
}
