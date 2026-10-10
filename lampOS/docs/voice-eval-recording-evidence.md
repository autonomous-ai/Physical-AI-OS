# Recording evidence used by voice-eval

An acoustic annotation needs the actual continuous room WAV. A matching hash
string in two JSON files is not recording verification. `voice-eval` opens,
checks and hashes `room.wav` beside the observer's final `metadata.json` before
retaining valid room evidence. A physical collection also requires a successful
recorder process exit; a timeout, signal or nonzero exit cannot reuse a stale
`valid: true` report. Trace imports lack that process receipt and retain
`recorder_success: null`; they still require a valid final observer report and
the actual WAV.

`import::load_room_evidence` retains invalid metadata/audio with
`valid: false` and a `verification_error`. Missing or corrupt room evidence does
not drop the attempt or erase its software events. A valid receipt records an
absolute `wav_path`, the claimed recording hash, and `verified_wav` with the
actual SHA-256, byte count, frames, rate, channels, sample format and duration.
When the observer reports requested/written frame counts, format, duration,
sequence gaps or errors, those claims must agree with the verified file.

The reader accepts complete little-endian RIFF/WAVE containing one PCM16,
PCM24, PCM32 or IEEE float32 data chunk, including the corresponding extensible
formats. It rejects missing/duplicate format or data, partial frames, truncated
chunks or padding, inconsistent RIFF length/rate/alignment, empty sample data,
and nonfinite float samples. Limits are 600 seconds, 8,000–192,000 Hz, 1–8
channels, 1 GiB plus 4 KiB total file bytes, and 4,096 chunks. Metadata is limited
to 1 MiB. These include the observer's current bounds. PCM data is streamed
through a fixed 64 KiB buffer, without decoding the whole recording into memory.
The existing `lamp-acoustic` reader is restricted to 16 kHz mono PCM16 and
60-second stimulus files, so it cannot verify the observer's multichannel
float32 recordings.

The restricted verifier is not a hound decoding wrapper. In pinned hound 3.5.1,
`WavReader::new` discards the RIFF length returned by `read_wave_header`;
`read_until_data` stops at the first data chunk and ignores chunks after it.
Reading every sample can detect a truncated sample payload, but does not
certify the complete file length, trailing chunks or duplicate data. Its float
sample reader also accepts nonfinite IEEE values. Those properties require
additional checks even if hound supplies sample decoding. Here a single
bounded streaming pass checks all chunks, hashes the same bytes, and checks
float finiteness without converting integer samples or allocating decoded PCM.
Hound is used by the tests to create standard and extensible fixtures, not
claimed as the production verifier; SHA-256 and safe descriptor opens use the
existing sha2 and rustix dependencies.

Only regular files are opened. The final path component cannot be a symlink;
nonblocking open also prevents a substituted FIFO from waiting for a writer.
Parent directories are operator-selected evidence locations, not a filesystem
sandbox. Verification checks a 30-second budget between bounded reads. A native
filesystem call cannot be preempted; a filesystem hang still requires an
external process deadline. No audio device, network, player or synthesizer is
opened by verification.

`Annotations::score` and `reviewed_step` reverify the WAV when used, including
when a report is regenerated from an old ledger. Prior `valid` or
`verified_wav` fields never skip this read. Removing or changing the recording
after import therefore makes its annotations unscored. Metadata-only old
ledgers without a verifiable path are unscored; re-import retained original
artifacts to obtain a verified receipt. Moving recordings does not silently
retarget a ledger's absolute path. Offline scoring currently hashes once for
the attempt's acoustic scores and again for each requested content review;
this favors correctness over caching and never runs on the audio input path.

Annotations must use the matching run ID, attempt ID and actual WAV hash, with
`listened: true`. Each supplied acoustic boundary must be finite and within
`[0, verified_duration]`; uncertainty must be finite, nonnegative and at most
the recording duration. Invalid values are unscored even when annotations were
constructed directly in Rust rather than parsed from JSON. Reversed boundary
pairs remain unscored timing, but do not erase an independent content judgment
when the recording, identity and individual values are valid. Software
`hint_stimulus_start_s` is ignored for scoring. A missing endpoint is not a
zero-latency observation.

These checks establish file identity and structural integrity. They do not
prove that a recorder used the claimed physical microphone, that an annotator
actually listened, that speech is intelligible or correctly attributed, or
that an answer is complete/correct. They do not independently reconstruct
callback continuity from the observer ledger. Observer capture validity and
human review remain separate evidence, and software timestamps never become
acoustic boundaries. Tests use synthetic local PCM files only; they are not
physical or conversational acceptance evidence.
