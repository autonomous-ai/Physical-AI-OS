# AEC alignment evidence

Directed mode still accepts VAD immediately. This change adds observations, not
an echo classifier, an AEC tuning change or a verified full-duplex improvement.

## Keep three quantities separate

| Quantity | Meaning | What it does not establish |
|---|---|---|
| `aec_queue_delay_ms` | Supplied hint derived from ALSA queue observations and nominal sample rates. | Unknown hardware/DSP delay, measured ADC time or fixed cancellation alignment. |
| `aec_internal_alignment_ms` | Sonora's cached render-buffer alignment after that capture block. | Convergence, confidence, physical propagation time or absence of echo. |
| Recorded waveform alignment | An offline match between accepted render and microphone PCM. | Acoustic playback/capture timestamps or the correct value for either field above. |

Pinned Sonora 0.2.0 enables AEC3's internal delay estimator. Calling
`set_stream_delay_ms` does not command steady alignment. The hint can seed the
render buffer at first capture or after internal resets; subsequent internal
estimates can override it. The previous 0/80/100/120 ms experiment varied hints,
not forced reference offsets. Its inconclusive result does not prove timing is
irrelevant or justify copying V1's 205 ms default. V1's native binding has not
been equated with AEC3.

`EchoProcessor::internal_alignment_ms()` reads an already-computed statistic.
It returns `None` before the first successful capture and after a full processor
reset. A present value may still be initialized or retained state. It is derived
from circular-buffer alignment in 4 ms blocks, not an average. Sonora's separate
median/stddev fields are not populated in the inspected version.

## Recording and replay

Only explicit audio diagnostics read this field in the live capture worker.
It uses the existing bounded diagnostic writer, alongside capture sequence, DSP
epoch, reference cursors and original timestamps. The microphone/coordinator
wire contract and admission policy are unchanged. No extra DSP pass, optional
echo detector, device operation or cloud call is added. The getter's host target
is below 10 microseconds; native capture deadlines still need a regression run.

`lamp-audio replay-aec` preserves an original value, when present, in each
`capture_pair.meta.aec_internal_alignment_ms`. Older recordings keep the field
absent. A sibling `replayed_internal_alignment_ms` object contains separately
computed `aec_only` and `aec_ns` values from that replay. These are not original
device measurements. Replay uses the recorded queue hint and call order, never
the observed internal alignment as a control input.

## Source audit and recorded evidence

The audit compared pinned V1-main `d5efe9d7b73cc529b34cd4abe97624682a82ca94`
with Rust checkpoint `9c2c82e166fe10b690268392fef458b9e79f7abf`. Cached Sonora
sources were byte-checked against crate archives matching Cargo.lock hashes.

- The 24 kHz render / 16 kHz capture configuration uses a persistent resampler.
  No float normalization or sample-rate mismatch was found.
- Rust rebuilds APM on a DSP reset; V1's application-buffer reset keeps its
  processor. V1 also bypasses processing after idle. Cold and warmed trials
  need separate labels. The continuous-clock repair already avoids a full
  rebuild on ordinary reply cancellation.
- Stock V1 suppresses input during playback. Stable replies in that mode do
  not establish natural interruption behavior.

An offline analysis of original PCM, without rerunning AEC, compared certified
fixed-reply `h4jds57i` and NS-off recordings. In a predeclared 300–400 ms window
after first accepted reply, microphone/render normalized correlations were
-0.965863 and -0.967648; the sign indicates inversion. Read-relative matching
offsets were approximately 113.50 and 113.25 ms. These are signal matches, not
probabilities or acoustic latency. They support checking alignment state, not
replacing the queue hint with 113 ms. No clean direct-human overlap cohort is
available, so no rejection threshold was selected.

Private source snapshots, input hashes, original-PCM analysis and validation
receipts are retained under `artifacts/aec-alignment-20261010/`. Raw recordings
are not committed. An initial analysis with Python 3.14/NumPy 1.26 failed numeric
validity checks and was discarded. The retained analysis used Python 3.12/NumPy
2.2, with selected correlations independently checked by scalar math. This
private analysis is not a runtime dependency; lampOS remains Rust-only.

## Verification boundaries

The DSP regression compares observed and unobserved processors sample for
sample, including reset invalidation. Replay tests cover legacy absence,
preserved original values and unchanged results when metadata changes. The
recorder test checks that queue hint and internal alignment remain distinct
on disk. Host checks do not qualify the Linux worker, physical AEC, quiet overlap
recall or speech-end-to-first-word latency.

The source-only macOS x86_64 candidate passed 559 tests (zero failed/ignored),
formatting, strict workspace/all-target Clippy and release builds of `lamp-audio`
and `lamp-live`, using pinned Rust 1.96.0. Rust/Cargo source identity:
`173d1793fe09b229a75b29d8c9e7c3446baf8ff3e3a938b86834cc84ac1f3219`.
Commands were `cargo fmt --all -- --check`,
`cargo clippy --locked --offline --workspace --all-targets -- -D warnings`,
`cargo test --locked --offline --workspace -- --test-threads=1` and
`cargo build --release --locked --offline -p lamp-audio -p lamp-live`.
An 8,192-read release probe measured the cached getter plus host clock overhead:
first 80 ns, p50 31 ns, p95 32 ns, p99 33 ns, maximum 8,662 ns. This met the
host getter target; it excludes DSP, recording serialization and all hardware.

Inspect `sonora-0.2.0/src/audio_processing_impl.rs`,
`sonora-aec3-0.2.0/src/{config,block_processor,render_delay_buffer}.rs` and
`sonora-common-audio-0.2.0/src/push_sinc_resampler.rs` in the pinned Cargo sources
for these semantics. The [upstream project](https://github.com/dignifiedquire/sonora)
describes the AEC3 port; its current default branch is not a substitute for the
pinned source audit.
