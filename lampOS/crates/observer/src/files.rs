use crate::{RecordOptions, Result};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

static ID_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn new_id() -> String {
    format!(
        "{}-{}-{}",
        std::process::id(),
        lamp_ipc::monotonic_ns(),
        ID_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    )
}

pub struct Attempt {
    pub id: String,
    pub directory: PathBuf,
}

pub fn new_file(path: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
}

pub fn write_json(path: &Path, value: &Value) -> Result<()> {
    let mut file = new_file(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

pub fn json_line(writer: &mut impl Write, value: &Value) -> Result<()> {
    serde_json::to_writer(&mut *writer, value)?;
    writer.write_all(b"\n")?;
    Ok(())
}

impl Attempt {
    pub fn create(options: &RecordOptions) -> Result<Self> {
        Self::create_diagnostic(&options.output, &serde_json::to_value(options)?)
    }

    pub fn create_diagnostic(directory: &Path, requested: &Value) -> Result<Self> {
        fs::DirBuilder::new().mode(0o700).create(directory)?;
        let id = new_id();
        let initial = json!({
            "schema_version": 1, "attempt_id": id, "event": "attempt_created",
            "status": "pending", "valid": false, "requested": requested,
            "host_monotonic_ns": lamp_ipc::monotonic_ns(),
            "unix_time_ns": SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos(),
            "warning": "Only metadata.json may declare final capture integrity; absent final metadata is incomplete."
        });
        write_json(&directory.join("attempt.json"), &initial)?;
        let mut ledger = new_file(&directory.join("ledger.jsonl"))?;
        json_line(&mut ledger, &initial)?;
        ledger.sync_all()?;
        Ok(Self {
            id,
            directory: directory.to_path_buf(),
        })
    }

    pub fn finish(&self, report: &Value) -> Result<()> {
        let mut ledger = OpenOptions::new()
            .append(true)
            .open(self.directory.join("ledger.jsonl"))?;
        json_line(
            &mut ledger,
            &json!({"event": "attempt_finished", "report": report}),
        )?;
        ledger.sync_all()?;
        write_json(&self.directory.join("metadata.json"), report)
    }
}

#[cfg(any(target_os = "macos", test))]
pub mod sink {
    use super::*;
    use crate::capture::{Format, Packet, Shared};
    use rtrb::Consumer;
    use std::{
        io::{self, BufWriter},
        sync::{Arc, atomic::Ordering},
        time::{Duration, Instant},
    };

    pub struct Sink {
        wav: hound::WavWriter<BufWriter<File>>,
        ledger: BufWriter<File>,
        format: Format,
        frames: u64,
        packets: u64,
        next_source_frame: u64,
        sequence_gaps: u64,
    }

    impl Sink {
        pub fn new(directory: &Path, format: Format) -> Result<Self> {
            let wav = hound::WavWriter::new(
                BufWriter::with_capacity(64 * 1024, new_file(&directory.join("room.wav"))?),
                hound::WavSpec {
                    channels: format.channels,
                    sample_rate: format.sample_rate,
                    bits_per_sample: 32,
                    sample_format: hound::SampleFormat::Float,
                },
            )?;
            let ledger = BufWriter::with_capacity(
                64 * 1024,
                OpenOptions::new()
                    .append(true)
                    .open(directory.join("ledger.jsonl"))?,
            );
            Ok(Self {
                wav,
                ledger,
                format,
                frames: 0,
                packets: 0,
                next_source_frame: 0,
                sequence_gaps: 0,
            })
        }

        pub fn packet(&mut self, packet: &Packet) -> Result<()> {
            if packet.len > packet.samples.len()
                || packet.len as u64
                    != packet.info.retained_frames * u64::from(self.format.channels)
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid capture packet length",
                )
                .into());
            }
            if packet.info.source_frame_start != self.next_source_frame {
                self.sequence_gaps += 1;
            }
            for &sample in &packet.samples[..packet.len] {
                self.wav.write_sample(sample)?;
            }
            json_line(
                &mut self.ledger,
                &json!({
                    "event": "audio_callback", "wav_frame_start": self.frames, "packet": packet.info,
                }),
            )?;
            self.frames += packet.info.retained_frames;
            self.next_source_frame = packet.info.source_frame_start + packet.info.retained_frames;
            self.packets += 1;
            if self.packets == 1 || self.packets.is_multiple_of(64) {
                self.wav.flush()?;
                self.ledger.flush()?;
            }
            Ok(())
        }

        pub fn finish(mut self) -> Result<Value> {
            self.wav.finalize()?;
            self.ledger.flush()?;
            self.ledger.get_ref().sync_all()?;
            Ok(
                json!({"written_frames": self.frames, "written_packets": self.packets,
                "source_sequence_gaps": self.sequence_gaps}),
            )
        }

        pub fn consume(
            mut self,
            mut consumer: Consumer<Packet>,
            shared: Arc<Shared>,
            deadline: Instant,
        ) -> Result<Value> {
            loop {
                if Instant::now() > deadline {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "observer writer deadline",
                    )
                    .into());
                }
                match consumer.pop() {
                    Ok(packet) => {
                        self.packet(&packet)?;
                        shared.written_frames.store(self.frames, Ordering::Release);
                    }
                    Err(_) if shared.producer_stopped.load(Ordering::Acquire) => break,
                    Err(_) => std::thread::sleep(Duration::from_millis(1)),
                }
            }
            self.finish()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::{self, Format, Times};
    use std::{os::unix::fs::PermissionsExt, sync::atomic::Ordering};

    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("lamp-observer-test-{}", new_id())))
        }
    }
    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn fresh_attempt_cannot_overwrite_a_previous_attempt() {
        let temp = Temp::new();
        let options = RecordOptions {
            input: "synthetic".into(),
            seconds: 1,
            output: temp.0.clone(),
        };
        let attempt = Attempt::create(&options).unwrap();
        assert!(Attempt::create(&options).is_err());
        assert!(!attempt.directory.join("metadata.json").exists());
        let pending: Value =
            serde_json::from_slice(&fs::read(temp.0.join("attempt.json")).unwrap()).unwrap();
        assert_eq!(pending["valid"], false);
        assert_eq!(
            fs::metadata(&temp.0).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(temp.0.join("attempt.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        attempt
            .finish(&json!({"valid": false, "status": "test_failed_capture"}))
            .unwrap();
        let final_report: Value =
            serde_json::from_slice(&fs::read(temp.0.join("metadata.json")).unwrap()).unwrap();
        assert_eq!(final_report["valid"], false);
        assert!(attempt.finish(&json!({"valid": true})).is_err());
    }
    #[test]
    fn writer_deadline_retains_an_incomplete_attempt() {
        let temp = Temp::new();
        let options = RecordOptions {
            input: "synthetic".into(),
            seconds: 1,
            output: temp.0.clone(),
        };
        let attempt = Attempt::create(&options).unwrap();
        let format = Format {
            sample_rate: 16_000,
            channels: 1,
        };
        let (_, consumer, state) = capture::channel(format, 1).unwrap();
        let sink = sink::Sink::new(&attempt.directory, format).unwrap();
        let result = sink.consume(
            consumer,
            state,
            std::time::Instant::now() - std::time::Duration::from_millis(1),
        );
        assert!(result.is_err());
        assert!(!attempt.directory.join("metadata.json").exists());
    }
    #[test]
    fn synthetic_worker_preserves_pcm_channels_and_timing_ledger_without_devices() {
        let temp = Temp::new();
        let options = RecordOptions {
            input: "synthetic".into(),
            seconds: 1,
            output: temp.0.clone(),
        };
        let attempt = Attempt::create(&options).unwrap();
        let format = Format {
            sample_rate: 16_000,
            channels: 2,
        };
        let (mut input, consumer, shared) = capture::channel(format, 1).unwrap();
        input.receive(
            &[0.125_f32, -0.25, 0.5, -0.5],
            Times {
                host_monotonic_ns: 1,
                capture_stream_ns: 1,
                callback_stream_ns: 2,
            },
            |x| x,
        );
        shared.producer_stopped.store(true, Ordering::Release);
        let sink = sink::Sink::new(&attempt.directory, format).unwrap();
        let result = sink
            .consume(
                consumer,
                shared.clone(),
                std::time::Instant::now() + std::time::Duration::from_secs(2),
            )
            .unwrap();
        assert_eq!(result["written_frames"], 2);
        assert!(!shared.snapshot().valid_for(16_000));
        let mut wav = hound::WavReader::open(temp.0.join("room.wav")).unwrap();
        assert_eq!(wav.spec().channels, 2);
        assert_eq!(
            wav.samples::<f32>()
                .collect::<std::result::Result<Vec<_>, _>>()
                .unwrap(),
            vec![0.125, -0.25, 0.5, -0.5]
        );
        assert!(
            fs::read_to_string(temp.0.join("ledger.jsonl"))
                .unwrap()
                .contains("audio_callback")
        );
    }
}
