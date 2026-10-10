# Native component qualification — 2026-10-10

This is a component checkpoint, not a completed conversation runtime or a V1
speedup result. The running HAL and os-server were not replaced. The privacy
status reported software microphone mute, no input readiness and no playback.
No microphone, physical speaker, motor or ring output was exercised by these
benchmarks. Linux speaker tests used the ALSA `null` software device.

## Exact tested source

- Source content manifest: `d18479f4c067276440ce6e039ccad2c52bc7e1a07659fc7936261260fa0289e8`.
- Source archive SHA-256: `ac992a8ceaf8afe35df489a1db67027c23a8da4a551a101ddc86e6b91a58cb95`.
- Rust 1.96.0, Cargo.lock from that snapshot, optimized default release profile.
- Native host: Lamp `lamp-4ace`, Linux `5.15.147-sun60iw2`, AArch64.
- Private evidence: `artifacts/qualification-d18479f4c067/`, with per-file source
  hashes, raw benchmark JSON, executable hashes, dependency inventory and test
  receipts. Artifacts remain ignored; credentials were not recorded.

The five-crate source-only copy built outside the parent Git repository; all
local package and dependency paths resolved within that copy. The worktree and
extracted copy passed 93 tests each. Native Lamp passed 98, including four ALSA
`null` tests and SPI configuration validation. Formatting and strict clippy
passed on the Mac and Lamp. No Go or web sources were changed.

Exact gates, from the standalone project root:

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --release --locked -p lamp-audio -p lamp-ipc --bins
```

The extracted copy independently ran `cargo metadata --no-deps --format-version 1
--locked` with path containment checks and `cargo test --workspace --locked`.
Later source changes need new checks and identities; this checkpoint does not
qualify code added after the frozen snapshot.

## Measurements on Lamp

Three separate executions per tool, with all trials retained. Each DSP execution
used 500 warmup blocks followed by 6,000 measured blocks; each IPC execution used
1,000 round trips without warmup. Numbers below are ranges across trial-specific
percentiles, not a percentile of combined data. Nearest-rank percentiles are used.

| Boundary | p50 range | p95 range | p99 range | Maximum across trials |
|---|---:|---:|---:|---:|
| One 10 ms render + capture DSP block | 0.158–0.170 ms | 0.197–0.198 ms | 0.203–0.204 ms | 0.227 ms |
| Parent control send attempt → matching child reply, separate bulk queue full | 0.035–0.093 ms | 0.037–0.127 ms | 0.043–0.224 ms | 0.347 ms |

DSP missed its 10 ms processing budget in **0/18,000 measured blocks**. Cold
processor construction was 1.856–3.529 ms; the first block was 0.699–1.182 ms.
Fixture generation and signal-energy accounting are outside block timing. PCM
conversion, render processing and capture processing are inside it. This does
not include capture waits, scheduling between runtime workers, Gemini or output.

IPC completed **3,000/3,000** round trips with no control-send backpressure, clock
errors, forced child termination or unreaped child. Each sample checked that the
independent bulk queue was still full. The slower second trial is retained.
These are Unix process messages, not physical input or actuator response times.

The synthetic echo fixture produced 57.75 dB output-energy reduction. The separate
silent-render near-end **noise** fixture retained an RMS ratio of 0.231. Neither
number establishes speech intelligibility, double-talk quality, real-room echo
cancellation or a human's interruption being understood. Noise suppression
contributes to the energy reduction; do not call it a measured live-room ERLE.

The CPU governor remained `ondemand`; sampled frequencies varied from 416 MHz to
2.002 GHz, and thermal conditions changed across trials. Before/after sysfs and
load snapshots are retained. No fixed-frequency, fixed-core or isolated-system
claim is made. Per-process CPU counters were collected, but the collector's
`wait4` peak-RSS value may include pre-exec memory and is not an accepted measure
of complete V2 memory usage. Complete-process/cgroup profiling remains pending.

Commands for the frozen binaries:

```sh
lamp-audio bench-aec 6000
lamp-ipc-probe --samples 1000
```

## V1 comparison preparation

The [comparison protocol](comparison-protocol.md) compares only pinned V1 main
and Rust V2. Physical live conversation is the primary benchmark, including
speech-end to first substantive audible word, interruptions, missing answers
and unwanted responses. Modified V1 is excluded from evaluation.

The installed regular-file inventory contains 680 files and 201 Python package
versions. It includes the running os-server binary identity and service start
receipts; it does not prove which Python bytes were already imported before the
inventory. This inventory is historical deployment and rollback evidence, not
the V1-main baseline. Device pipes and other non-regular files are excluded.

Pinned main's AEC source has SHA-256
`771e366737c8d69754aa9d39d34a01d055cc84175488c20d8212143b2de62a4c`.
The installed legacy DSP dependency is `aec-audio-processing` 1.0.1; V2 uses
Sonora 0.2.0. A component comparison also needs disclosed settings, dependencies,
resampling and quality checks. It cannot establish a complete runtime upgrade.

Immutable shared capture/reference WAV tooling is available for component
diagnostics. The integrated Rust voice loop and paired live conversation runs
remain pending. No matched V1-main DSP or end-to-end V2 improvement is established
by this checkpoint.

## Cold-start measurement added after this checkpoint

The current `lamp-audio bench-aec` and `bench-aec-fixture` reporters also expose
`cold_start_echo` and `cold_start_near_end_with_silent_render`. Each has five
non-overlapping windows: 0–250 ms, 250–500 ms, 500–1,000 ms, 1–2 seconds and
2–5 seconds. They account for every early frame that the existing steady-state
quality fields exclude as warm-up. The near-end series begins after a full DSP
reset, with actual zero render input; it is independent of the echo series.

Each window reports input/output PCM RMS, their ratio and attenuation in dB.
Zero input has no preservation ratio; zero input or output has no finite dB
score. Those fields are `null`, rather than an invented infinite attenuation.
Negative attenuation remains visible. Energy accounting stays outside the
render-plus-capture processing timer, and fixture bytes/hashes are unchanged.
Generated and exported/reloaded fixtures must give identical window scores.

These are deterministic noise fixtures with a 70 ms direct echo path and a
77 ms reflection. They expose cold-start behavior of this DSP configuration;
they do not establish speech intelligibility, correct double-talk detection,
real-room echo suppression or the live worker reference-clock timing. The
historical numbers above remain tied to their original frozen source and did
not include these cold-window fields. New native evidence must identify its
own source revision and cannot inherit that earlier qualification.

The first native cold-window run used source
`6bcd44021b8768dd407a9f0b3cffdc473283da66edd3266b9e0f1a875b1e3b98`,
binary `e66178f1c169d458924a6867c27f7bcb64c260bf93d984140090fae28fe5a1b8`.
This is the previously frozen live source plus only the qualification reporter,
its tests and this document; concurrent reference-clock and motor edits are
excluded. Evidence is in `artifacts/cold-dsp-6bcd4402/`.

| Cold interval | Synthetic echo attenuation | Near-end noise output/input RMS |
|---|---:|---:|
| 0–250 ms | 50.06 dB | 0.238 |
| 250–500 ms | 47.88 dB | 0.244 |
| 500–1,000 ms | 44.99 dB | 0.250 |
| 1–2 seconds | 47.67 dB | 0.248 |
| 2–5 seconds | 54.98 dB | 0.231 |

Native `cargo test --locked -p lamp-audio` passed 56 tests. Workspace strict
clippy, formatting and the release `lamp-audio` build passed; the source-only
Mac copy passed 35 applicable tests, strict package clippy and formatting.
The native `bench-aec 6000` run reported steady processing p50 **0.167 ms**,
p95 **0.197 ms**, and zero blocks above 10 ms. This is one component run; it
neither opens the physical microphones/speaker nor demonstrates live turn
reliability. Strong early synthetic suppression means that generic cold DSP
warm-up alone does not explain the observed physical self-interruptions. The
live reference timeline and raw/processed microphone evidence still need
qualification. No V1-versus-V2 conversational speedup follows from this result.
