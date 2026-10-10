//! Finite, synthetic component timing only. No sensor, driver or device I/O.
use lamp_environment::*;
use lamp_interaction::{BootId, MonoTime};
use std::{hint::black_box, time::Instant};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    const COUNT: usize = 4096;
    let boot = BootId::new([1; 16])?;
    let mut cache = Environment::new(&[
        SourceConfig::new(Sensor::Scd41, boot, 15_000_000)?,
        SourceConfig::new(Sensor::Sen55, boot, 5_000_000)?,
    ])?;
    let initial = Readings {
        co2: Some(Co2Ppm::new(600.0)?),
        ..Readings::default()
    };
    cache.ingest(
        MonoTime::from_micros(0),
        Report::new(
            Sensor::Scd41,
            boot,
            1,
            MonoTime::from_micros(0),
            Event::Sample {
                readings: initial,
                device_status: DeviceStatus::NotReported,
            },
        )?,
    )?;
    let readings = Readings {
        temperature: Some(Celsius::new(23.0)?),
        humidity: Some(RelativeHumidityPercent::new(50.0)?),
        pm1: Some(MicrogramsPerCubicMeter::new(1.0)?),
        pm2_5: Some(MicrogramsPerCubicMeter::new(2.5)?),
        pm4: Some(MicrogramsPerCubicMeter::new(4.0)?),
        pm10: Some(MicrogramsPerCubicMeter::new(10.0)?),
        voc: Some(VocIndex::new(100.0)?),
        nox: Some(NoxIndex::new(1.0)?),
        ..Readings::default()
    };
    let mut elapsed_ns = [0u128; COUNT];
    for (index, elapsed) in elapsed_ns.iter_mut().enumerate() {
        let sequence = index as u64 + 1;
        let now = MonoTime::from_micros(sequence);
        let report = Report::new(
            Sensor::Sen55,
            boot,
            sequence,
            now,
            Event::Sample {
                readings,
                device_status: DeviceStatus::Reported(0),
            },
        )?;
        let started = Instant::now();
        cache.ingest(black_box(now), black_box(report))?;
        black_box(cache.snapshot(black_box(now))?);
        *elapsed = started.elapsed().as_nanos();
    }
    let first_ns = elapsed_ns[0];
    elapsed_ns.sort_unstable();
    println!("synthetic ingest + snapshot; sources=2, fields=9, n={COUNT}");
    println!(
        "first_ns={first_ns} p50_ns={} p99_ns={} max_ns={}",
        elapsed_ns[COUNT / 2],
        elapsed_ns[(COUNT * 99).div_ceil(100) - 1],
        elapsed_ns[COUNT - 1]
    );
    println!(
        "state_bytes={} snapshot_bytes={}; target_p99_ns=100000; hardware_unverified",
        std::mem::size_of::<Environment>(),
        std::mem::size_of::<Snapshot>()
    );
    Ok(())
}
