# lampOS handoff to Claude Code

Original export checkpoint: 2026-10-10, commit
`64529dee4d1228dd9373298a807d506f94421dca`, pushed to `origin/lamp-v2-chat`.
The owner initially requested the credit-saving handoff, then resumed the goal.
The broader goal remains unfinished; this document is not release acceptance.

## Resumed work after export

See [voice workstream ownership](docs/development-workstreams.md) before making
parallel edits or integrating another agent's changes.

- Fixed ring renewals exhausting the session trace after about 200 simulated
  seconds. Routine evidence is summarized; every hardware command and receipt
  check remains. Exact transition and tail samples preserve evaluator evidence.
  A dense 600-second simulated schedule fits the original cap. Combined source
  `962c876d` passed **768 host tests, zero failed or ignored**, formatting,
  strict Clippy and both release builds. See [ring renewal qualification](docs/ring-choreography.md#renewal-evidence-qualification-2026-10-11)
  for exact workloads, commands, source identity and limits. No device, ARM64,
  optical or acoustic qualification follows from this checkpoint.

- Fixed same-turn audio continuation losing an accepted final chunk and ending
  the session. Playback occurrences now retain separate tokens; whole-turn
  completion remains distinct. The runner waits for that completion, counts
  every segment and rejects malformed or incomplete evidence. New v2 plans
  keep historical v1 bytes unchanged. Combined source `6b0ea9e3` passed
  **753 host tests, zero failed or ignored**, formatting, strict Clippy and
  both release builds. See [playback occurrences](docs/playback-occurrences.md)
  for the original failures, exact commands, source identity and limits.
  No new ARM64, cloud, device or acoustic result follows. The subsequent ring
  renewal trace-capacity correction is recorded above.

- The preceding checkpoint made Gemini and fixture conversations share the
  optional lifecycle cue channel.
  Its runner originally triggered follow-ups from a single retirement; the
  new occurrence correction above replaces that incomplete assumption.
  Ownership, expiry and original observation times remain preserved.
  Combined source `5a827d27` passed **724 host tests, zero failed or
  ignored**, formatting, strict Clippy and both release builds. See
  [directed cue integration](docs/directed-cues-integration-20261011.md) for
  exact commands, source identity and the simulated/physical distinction.
  No new ARM64, cloud, device or acoustic qualification follows.

- The preceding evaluator integration includes Agent 2 through `3eaa0365a`
  (42 scenarios, 21 evaluator canaries), plus prompted-human scoring and
  historical-plan fixes.
  Source `30263f21` passed **712 host tests, zero failed or ignored**,
  formatting, strict Clippy and both release builds. See
  [integration evidence](docs/voice-eval-integration-20261011.md) for the full
  identity, exact gates, reproduced failures and remaining qualification.
  This adds no ARM64, cloud, device or acoustic result.

- Agent 1 delivered Gemini reliability commit `fbf9ecc3c` on `gemini-voice`,
  directly above `23bde4873`. This checkpoint integrates it with
  [provider output credits](docs/provider-flow-control.md), reserving PCM space
  before sending while preserving capture, control and 100 ms IPC freshness.
  The coordinator's 30-second cap is a backlog limit; long answers no longer
  depend on the root accepting unreserved PCM. This is not yet sustained
  end-to-end qualification: the upstream two-second delivery watchdog still
  needs Agent 1's playback-paced regression. See the linked protocol and
  [next 24-hour assignments](docs/development-workstreams.md).

- The earlier combined source `670a905d` passed **696 host tests, zero failed or
  ignored**, formatting, strict workspace/all-target Clippy and release builds
  for `lamp-live` and `lamp-voice-eval`. The full source manifest is
  `670a905dfb3b01bd5724b98e8a8f38fc4787f46dc81f895ad6d6358ff6b411af`. Exact commands and
  retained failures are in [provider flow control](docs/provider-flow-control.md)
  and `artifacts/provider-flow-20261011/`. No new cloud, ARM64 or physical result
  follows from this host qualification.
- Evaluator integration now separates [completed playback and content review](docs/voice-eval-answer-review.md)
  and [verifies room WAV evidence](docs/voice-eval-recording-evidence.md) before
  acoustic scoring. Known irrelevant/incomplete or duplicate/split answers do
  not improve completion rates, and missing/changed recordings remain unscored.
  Unreviewed playback still cannot establish semantic success. Directed-mode
  cues are integrated; playback-paced upstream delivery, relay framing, and
  physical admission/echo qualification remain open in the workstream notes.

- [AEC alignment diagnostics](docs/aec-alignment.md) now record Sonora's cached
  internal alignment separately from the supplied queue hint, only with explicit
  audio diagnostics. Replay retains original values and reports its own values
  separately. This does not change DSP settings or interruption policy. The
  source audit found no rate/normalization mismatch and no proven acoustic fix;
  V1's 205 ms default is not a justified replacement for the current hint.
  Source `173d1793` passed 559 host tests, formatting, strict Clippy and release
  builds; exact commands, getter overhead and remaining native/device checks
  are in the linked document. Evidence: `artifacts/aec-alignment-20261010/`.

- Immediate owner priority is complete voice interaction. Codex owns microphone/
  AEC, candidate admission, choreography and integration. The owner requested
  external Claude sessions for Gemini reliability (`crates/gemini/**` and
  `provider_worker.rs`) and the Rust voice acceptance runner (`crates/voice-eval/**`).
  Agent 1 has delivered the commit noted above. Agent 2 delivered the Rust
  evaluator through `3eaa0365a` (ten commits preserved), including 42 scenarios,
  scripted fault injection and complete attempt reports. Integration review
  found answer-completion accounting and room-recording provenance defects;
  their fixes and final host verification are part of this checkpoint.
  Environmental acquisition is deferred behind voice.
- The [input admission boundary](docs/input-admission.md) separates detected
  candidates from destructive cancellation with bounded original audio, scoped
  evidence and explicit rejection. Directed mode remains immediate VAD-only;
  this is not an echo classifier or a physically verified interruption fix.
  The combined source passed 557 host tests, formatting, strict all-target
  Clippy and release build; Rust source identity `8176c475` (full hash and
  boundaries in the linked document). Evidence is in
  `artifacts/input-admission-20261010-8176c475/`; its separate durable backup
  is `resumed-admission-20261010-8176c475/`. It remains unqualified on ARM64
  and the real Lamp.

- Latest owner steering: work without the physical device until they check it
  in the office tomorrow. Do not continue discovery, SSH, audio or movement
  experiments while that restriction applies. Local builds/replays continue.

- The owner requested an evidence-based V1/ROS review and reuse of suitable
  existing technology. See [reuse decisions](docs/reuse-decisions.md).
- Lamp's previous address is unreachable. The authorized full local-subnet scan
  found no matching 4ace host key or public identity. Other Lamps were not
  authenticated or changed. See [discovery result](docs/benchmark-progress.md#resumed-device-discovery-2026-10-10).
- The newest terminal-shutdown fix still has no physical regression run.
  No fresh latency/speedup or successful-overlap claim follows from local work.
- A fresh iMac photo at `2026-10-10T14:51:51Z` was too dark to verify Lamp's
  presence or placement. Camera access works through the existing app.
  This is not permission or evidence to move the robot.
- The new [environmental snapshot contract](docs/environment-snapshots.md)
  ports V1's useful per-field freshness and source semantics into fixed-size
  Rust state. It has no driver, bus access or runtime integration. Its 12
  focused regressions and the full 491-test macOS workspace run passed,
  with strict workspace/all-target Clippy and formatting. Native ARM64 and
  installed-sensor qualification remain pending.
- The private optional-overlap helper is frozen and offline-reviewed at
  `/private/tmp/lamp-fixture-overlap-v2-7okz9prt`, receipt SHA256
  `d2d07540f1ab036cd5d2bc37e2ee1ba3f9738bda60360f21d8ce0f3322a3503a`.
  It reuses the cached topic-change utterance and passed 54 test executions
  (39 unique tests). It has not run on Lamp. Production audio code, gain
  and admission thresholds have not changed.
- The optional [conversation/ring integration](docs/ring-choreography.md) now
  uses a separate writer and shared Controller ownership. Static phase cues
  follow admitted input and matched playback, with bounded data/control work,
  original leases and explicit black-write shutdown evidence. This is local
  host-qualified work, not deployed or physically qualified; the expressive
  head/body/camera/sensor experience remains unfinished.
  Its final source-only macOS run passed 535 tests, strict workspace/all-target
  Clippy, formatting and the host release build. The separate-process fake-sink
  probe passed 120 samples (request-to-write p95 3.177 ms, not optical/voice
  latency). Exact source manifest and receipts are in
  `artifacts/ring-live-20261010-50cb160b/`. Linux/ARM64 gates are pending because
  the local VM did not remain running. This source is not covered by the earlier
  native checkpoint.
- The owner has now authorized a shared commit/push of the verified resumed
  work for two other agents. It has not been deployed. Identify the resulting
  checkpoint with `git log -1 -- lampOS/`; do not confuse host validation with
  the earlier native checkpoint. Earlier environmental/helper work is backed up in the export
  backup's `resumed-20261010/` directory. The later ring slice, full current
  source and test evidence have their own `resumed-ring-20261010-50cb160b/`
  directory and manifest. Neither replaces the original export or its receipts.

The sections below preserve the original export's qualification and device
checkpoint. Later source additions do not retroactively become part of its
518-test native build or its frozen source identity.

## Read this first

- Authoritative project: `/Users/autonomous/harnesses/worktrees/autonomous-os/lamp-v2-chat/lampOS`.
  Do not resume in `calm-pebble` or the old modified V1 checkout.
- Branch: `lamp-v2-chat`. Base commit / pinned V1-main comparison source:
  `d5efe9d7b73cc529b34cd4abe97624682a82ca94`.
  The owner explicitly authorized exporting this checkpoint to
  `origin/lamp-v2-chat` in `autonomous-ai/autonomous-os`. Source and English docs
  are included; recordings, caches, build output and private receipts stay local.
  Use `git log -1 -- lampOS/` to identify the checkpoint commit. No merge into
  `main` or fleet release is part of this export.
  Commit review excludes binary artifacts and found no credential-pattern matches.
  `git diff --cached --check` reports one preserved upstream blank line at EOF in
  `vendor/linux-uapi-v6.12-adc218676eef/LICENSES/Linux-syscall-note`; its checksum
  matches the vendor manifest. The check passes when excluding that exact file.
- Treat lampOS as its own repository. Runtime and release-owned tooling are Rust;
  source, dependencies, configuration, assets and docs must be self-contained.
  External Linux drivers, Rust crates, model files and Gemini are explicit dependencies.
- All notes, docs and comments are **English only**, within this directory.
  The owner's explicit instruction overrides the parent EN+VI convention.
- First release: **chat with a physical robot at a desk**, fixed on the left or
  right of one computer user. Voice, head, body, light, camera and sensors should
  act coherently. No Harness/agentic tasks or new long-term memory infrastructure
  in this release. Those were earlier design discussions, not current scope.
- Follow [AGENTS.md](AGENTS.md), [architecture](docs/architecture.md), and
  [hardware contract](docs/hardware-contract.md). Purposeful speech, listening,
  movement and silence; no stock waiting fillers, automatic room scans or
  mandatory motion per reply. Natural topic changes should interrupt without a
  special "stop talking" command.
- The owner already authorized ongoing SSH/SCP/deployment and iMac audio/camera
  testing. Do not repeatedly ask for the same authorization. Still honor actual
  OS privacy consent. Do not commit or push without a separate explicit request.
- **Motors must remain inhibited.** Lamp is folded on a cabinet. There is no fresh
  verified view of the whole placement, cabinet edge and joint clearance. Do not
  move, torque-enable, scan or remove the serial-device inhibit on the strength
  of old photos. Motion requires current placement and safety evidence.
- Installed legacy services were restored after physical tests. V2 is a finite
  qualification runtime, not the installed default or an accepted product.

## Current result in plain English

The same Lamp can capture microphone audio while playing a complete answer.
The old cancellation-clock failure has been repaired and physically exercised.
This is useful progress toward natural interruption, but it does **not** prove
reliable full-duplex conversation: Lamp can still interpret residual playback as
a new user turn and interrupt itself.

The newest audio fix addresses a separate terminal shutdown race. It has not
had a physical regression run. After the owner resumed work, the new local
sensor contract and test-helper checks did not change that audio result.

**There is no accepted V1-main/V2 p50, p95 or speedup.** An early V1 reply measured
4.39–4.45 s and an early V2 reply 1.945–2.015 s, each n=1 with different Gemini
settings and unresolved issues. Do not present these as a Rust speedup. Several
later complete answers exist, but a matched acoustic cohort is still pending.
See [benchmark progress](docs/benchmark-progress.md).

## Code and qualification checkpoint

| Item | Exact state |
|---|---|
| Latest physical binary | Source `e4ff39d998caa1eb6c503e408f91b7184ea10c3c7b92e06b861d82ea08657071`; 514 native ARM64 workspace tests passed, plus strict Clippy, formatting and release build. |
| Latest local code | Source `36566770ec2a8531a4a3ea85373ea76237994dd8a62a826ab8b8d474767998e4`; adds the terminal reference-close fix below. |
| Latest native gates | **518 native ARM64 tests passed**, zero failed/ignored; strict workspace/all-target Clippy, formatting and release build passed. See `artifacts/native-live-terminal-36566770/`. |
| Latest physical rerun | **Not performed for source 36566770.** The earlier failed NS-on recording remains invalid. |
| Exported source identity | The source snapshot above describes the exported audio checkpoint. Resumed environmental-contract additions are separate, host-verified work and are not included in this native qualification. |

Newest frozen qualification:
`/private/tmp/lampOS-combined-tail-camera-di_at_fy`.
Archive SHA256:
`26ef0d46eb418ffb19f6afa6411c73af34cfe4b6b52fc2a874da2e08b860bea3`.
Native directory:
`/home/orangepi/.local/share/lampOS-build/live-26ef0d46eb`.

Prior physical qualification:
`/private/tmp/lampOS-combined-tail-camera-5ja4y5mj`.
Native binary:
`/home/orangepi/.local/share/lampOS-build/live-a27c144f8f/lamp-live`.
Binary SHA256:
`1368d7e108bf6cb246f09ebc71178e444c56c9e17ade9738299d87484ffcc6bf`.

### What the three defects actually were

1. **Playback supply gaps broke the reference clock.** Capture continued while
   the speaker stopped supplying accepted PCM between speech chunks. The fix
   writes actual accepted silence while preserving owed speech and ownership.
   Subsequent physical recordings exercised the gap path without that failure.
   Local scheduling was also changed to dispatch already queued speech promptly.
   Genuine cloud supply gaps still exist; no default startup buffering was chosen.
2. **Ordinary cancellation broke the reference clock.** Stopping/restarting the
   speaker while capture continued caused a reference gap/reset failure. The fix
   rejects old-owner writes, retires only the bounded already-queued tail, and
   keeps actual zeros and the reference clock running. Privacy/fault paths still
   hard-stop. The physical `h4jds57i` recording ran all 30 s with 3,001 contiguous
   capture and 3,003 render blocks, no cancellation reset or reference fault.
   Its 288-frame queued tail (12 ms nominal) retired 14.174 ms after the local
   cancellation trace. **That is an ALSA/software boundary, not audible stop latency.**
3. **Final shutdown could turn a clean close into a fault.** In the later NS-on
   fixture, capture handled terminal privacy denial and closed its reference
   socket before speaker sent `ReferenceControl::Stopped`. The strict send got
   `ECONNREFUSED` after the local speaker stop/reset had already succeeded.
   Render diagnostics correctly certified a fault; this was not an ACK timeout.
   The new fix makes only this final notification a single optional nonblocking
   send over an already connected control socket. No connect/retry/wait or fake
   ACK; live control/data sends and diagnostic certification remain strict.

The newest fix changes exactly:

- `crates/live/src/audio_workers.rs`
- `crates/live/src/transport.rs`
- `crates/live/tests/reference_link.rs`
- `docs/live-runtime.md`

It passed 33 focused host tests, strict live all-target Clippy, formatting and
patch checks before promotion. Four new Unix-socket tests cover capture-first,
speaker-first, never-connected and control-only-connected shutdown orderings.
Linux audio workers are excluded on macOS, so native gates matter.
See `artifacts/terminal-reference-close-20261010/` and the final native receipts.

## Main unresolved issue: false interruptions

`crates/live/src/activity.rs` currently starts a turn after six consecutive
10 ms post-software-AEC blocks with speech probability >=0.80. It retains 300 ms
pre-roll, ends after 600 ms silence, and caps an utterance at 120 s.
The coordinator treats `Activity::Start` as a new owner, cancels the current
reply and clears old PCM. There is no qualified near-end/addressee/echo gate.

In fixed-reply `h4jds57i`, one cached iMac greeting and one cached Lamp reply were
scheduled, with Gemini disconnected. A second input was admitted 846.505 ms after
the first accepted reply write; only 20,400 of 107,760 reply frames were accepted.
The clock now survived, but the answer was still incorrectly cancelled.

Compared with the prior fixed-reply failure, the trigger-window pre-AEC waveform
correlation was 0.998666 and post-AEC/NS correlation 0.808681, aligned to the same
cached reply segment. These are waveform correlations, not confidence probabilities.
Evidence strongly implicates playback residue. It does not isolate the physical
board DSP, room acoustics, software AEC or NS as the sole cause, or rule out all
other room sounds. Offline ASR is retained as hypotheses, not proof of silence.

Later, with fresh equal mixer readings, an NS-off/on pair played the full fixed
reply in both runs with no extra admission or speech gap. NS-off certified clean;
NS-on failed terminal render finalization through defect 3 above. Their maximum
post-playback VAD was 0.752864 / 0.699609. Neither reproduced h4. **No NS winner**
is justified by this pair, and it does not qualify genuine human overlap.

Other completed investigations:

- A native ARM64 delay replay matrix found no consistent winner among added
  0/80/100/120 ms delay hints. Keep current delay unchanged.
- An observation-only native echo-metric probe preserved original PCM, VAD,
  admission and reference/reset lineage exactly across ten passes. The residual
  likelihood was zero through most trigger frames in some failures, while high
  values also occurred in non-cancelled answers. ERL/ERLE initially exposed held
  values. No fast echo threshold was selected and no production gate was added.
- Native replay exactness is essential: macOS AVX2/FMA replay differed from the
  original ARM64 NEON execution. Do not use host numeric divergence as a DSP bug
  or declare native equivalence from host tests.
- A proposed two-case opposite-NS replay was **stopped before implementation**.
  Its plan is in the durable backup at `offline/paused-two-case-replay.md`.
  There are no new results for it. It is optional follow-up, not a completed fix.

V1-main defaults usually discard microphone input during TTS and suppress an
echo tail, avoiding this failure by also limiting natural interruption. Optional
V1 full-duplex paths exist but were not its stock configuration. Preserve that
distinction. [V1/V2 microphone audit](docs/microphone-v1-v2-audit.md) documents the
actual pinned code, rather than assuming V1 had the same listening behavior.

## Hardware and current setup

- Team layout: three physical capsules, two host capture devices. Center mic1
  uses C-Media / Unitek Y-247A for ambient sensing. Outer pair mic2 feeds Jieli
  onboard processing and is used for conversation.
- Host Jieli output is mono 48 kHz. `pre_aec` is before lampOS **software** AEC,
  potentially after board DSP; it is not separate raw capsules. Neither software
  direction finding nor hardware playback-reference wiring is established.
- Speaker route uses C-Media. Separate C-Media/Jieli USB clocks are not a
  synchronized microphone array. Use vision and desk calibration as distinct
  attention evidence; do not invent direction data.
- The team reports clearer direct human speech than re-recorded loudspeaker
  speech. Keep live-human and iMac-replay cohorts separate. Two synthetic voices
  from one speaker do not prove spatial multi-person handling.
- Last verified gains: Jieli capture 147, hardware AGC on; C-Media mic capture 4,
  AGC off; hardware playback 9,9; `device_speaker` soft volume 75 (-7.5 dB).
  No release gain/NS/delay change was selected. Capture rails appeared briefly
  early in playback in the latest pair; inspected trigger windows did not clip.
- `device_micro1` is an ALSA PCM/control alias, **not** its card ID. Use
  `amixer -D device_micro1 ...`; the actual C-Media card ID is `device_cmedia`.
- `/etc/asound.conf` SHA256:
  `1c1873c5319b729f6a324be3b6e258c922b04648d4980edb79a97c8342e6442d`.
  Existing observer dsnoop setup must be recorded consistently; do not overwrite it.
- Mac playback uses explicit `iMac Speakers`, 48 kHz float stereo, volume 65,
  unmuted at the last physical run. Verify current state when resuming.
- Mac microphone permission was unavailable while the Mac was locked. The
  temporary continuous room observer is Lamp's C-Media ambient input. It shares
  Lamp's host/card with playback and is **not an independent external recorder**.
  Do not claim the iMac mic recorded these trials or bypass macOS TCC.

Device: `orangepi@172.168.20.159` (lamp-4ace), not `pi@...`.
Existing connection template, without credentials:

```sh
/usr/bin/ssh -S /private/tmp/lamp-4ace-voice-ssh \
  -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/private/tmp/lamp-4ace-known_hosts \
  orangepi@172.168.20.159 'READ_ONLY_COMMAND'
```

The control socket may expire. Do not copy it or print passwords/tokens. Use the
authorized credential mechanism if reconnection is needed. `sudo -n` worked.
Private provider configs live on the device under
`/home/orangepi/.local/share/lampOS-build/provider-private-8eexrs09/provider.json`
and `provider-matched-vx_xbmxi/provider.json`. Do not print or commit them.
The matched extended-thinking/LOW/Kore/en setup qualified provider setup, not an
accepted matched conversation benchmark. See [comparison config](docs/gemini-comparison-config.md).

Preserve `/run/systemd/system/hal.service.d/90-lampOS-stationary.conf`:

```ini
[Service]
InaccessiblePaths=/dev/ttyACM0
```

Installed `os-server.service`, `hal.service`, `hermes-gateway.service` should be
active. `lamp-v1-main-hal.service` and `lamp-v1-main-os.service` should be stopped.
HAL's running mount namespace should report servo device `0:0` mode `0`; physical
`/dev/ttyACM0` should have no owner. This `/run` safeguard is not reboot-persistent;
do not assume a rebooted board remains inhibited. Avoid an unplanned reboot.
Final read-only check at `2026-10-10T13:23:39.477446+00:00` verified all three installed services active, both V1-main services inactive, expected HAL motor mask, no servo owner, no remaining V2 runtime process, audio/voice/TTS health true and servo health false. Receipt: `artifacts/handoff-20261010/device-check.json`.

## Evidence and reusable assets

Durable private local backup:
`/Users/autonomous/.local/share/lampOS-handoff/2026-10-10`.
Its `manifest.json` maps every original path to a relative saved path and records
file size and SHA256. Nothing was uploaded. It excludes build targets and cloud
credentials. Preserve raw recordings privately; do not put binary artifacts in Git.

| Saved subdirectory | Contents |
|---|---|
| `trials/h4jds57i` | Physical cancellation-continuity recording, traces and restoration receipts. |
| `trials/7v7pvbyl` | Earlier failing fixed-reply recording for comparison. |
| `trials/ns-pair` | Fresh-gain NS-off/on pair, including the invalid NS-on evidence. |
| `qualification/physical-e4ff39d9` | Frozen source and native receipts for the physical binary. |
| `qualification/shutdown-36566770` | Frozen latest source and final native receipts. |
| `tools/fixed-reply-harness` | Finite fixture runner, qualification checks, cleanup and restoration helpers. |
| `tools` | Native qualification/freezing, thermal check, restoration, conversation and pair helpers. |
| `offline` | Frozen echo/delay experiment archives and the paused two-case proposal. |
| `bin/lamp-observer` | The hash-pinned Mac playback binary used in the recordings. |
| `cache` | The existing acoustic asset cache and exact cached Lamp reply, copied without regeneration. |
| `evidence` | Latest native, cancellation, NS, echo-metric, V1 audit and restoration receipts. |
| `project-notes` | This handoff, entry-point rules and the current English documentation. |

External Python helpers are **diagnostic-only**, outside lampOS; they are not a
Python runtime dependency or a permanent release adapter. Several contain old
absolute `/private/tmp` paths. Inspect and relocate through the manifest before
reuse, using fresh output directories; do not blindly replay stale paths or
rerun a script that requires a new remote directory. The source freezer's root
file allowlist predates `HANDOFF.md`/`CLAUDE.md`; include them in future snapshots.

The existing voice cache is in `lampOS/.cache/acoustic/` (24 utterances, 20 scenes,
44 cached assets at creation). Keep it; do not regenerate to resume testing.
The greeting is Daniel at 190 wpm with 1.912 s speech in a padded 20 s file:

- Original `.cache/acoustic/objects/19866296e453d03f8689a2996377ef70251e986c1c514caffa6dc2fe9bf37d1d/audio.wav`
- SHA256 `29b5d2a18fe92163b0fbb4cbddd7fff105fc329ebfcbdb165b33c7841a9985f3`
- An exact copy is also retained as `trials/h4jds57i/stimulus.wav` in the backup.

Cached actual Gemini reply: 107,760 frames, 4.49 s, 24 kHz mono, extracted from
accepted speech PCM with explicit supply-gap zeros removed. This is a fixed
regression stimulus, not a new generated answer:

- Device `/home/orangepi/.local/share/lampOS-build/reply-fixture-76x62xf9/reply.wav`
- Local cache `lampOS/.cache/acoustic-replies/` and original
  `/private/tmp/lampOS-cached-gemini-reply-7wp5442n/reply.wav`
- SHA256 `d5bd0d6f880434b81846accf5181b9bf5ab28ac91479827aa9f73ddf8b52e144`
- Text: "I'm doing well, thank you. I'm ready to chat or help with anything you need today."

Mac player SHA256:
`a27a91564621fb4665a07d918db975daf3b01fe6e043fb659e0f8b83af813588`.
The fixture runner supports `--player` and `--stimulus` for relocated exact assets.
The original exported helper schedules no second cue. The separately retained
optional-overlap revision above supports `--secondary topic-change` with
`--secondary-delay-ms 0|600|800|1200`. It uses authenticated bounded cues
from the owned runtime and rejects expired dispatches. Once the new utterance
starts, ordinary old-answer cancellation must let it finish; pending cues
are withheld on cancellation. Runtime exit/fault/deadline still stops it.
There is no dedicated external privacy cue, so it does not claim immediate
GPIO-to-iMac silence. Actual overlap and audible stopping require room-audio
scoring. The one-reply fixture does not test a complete second answer.
Read that helper's README and verify its `ready.json` before use. Do not run
it until 4ace identity, motor inhibit, device state and restored services
can be freshly verified. No physical overlap result exists yet.

Primary local evidence directories (ignored by Git but retained on disk):

- `artifacts/native-live-tail-camera-e4ff39d9/`
- `artifacts/fixed-reply-clock-continuity-20261010/`
- `artifacts/fixed-reply-ns-pair-20261010/`
- `artifacts/terminal-reference-close-20261010/`
- `artifacts/native-live-terminal-36566770/`
- `artifacts/native-echo-telemetry-20261010/`
- `artifacts/native-echo-delay-matrix-20261010/`
- `artifacts/v1-main-microphone-audit-20261010/`

Each retained comparison needs source identity, actual settings, all failures and
full diagnostics. A successful process exit or transcript is not conversation
acceptance. Invalid render diagnostics remain invalid even if most audio exists.

## Build and resume mechanics

Pinned Rust is 1.96.0. Run from lampOS, or from a source-only standalone copy.
Use a fresh native target directory for each frozen qualification; a stale
source/target mix previously produced misleading validation.

Native build environment on Lamp:

```sh
export RUSTUP_HOME=/home/orangepi/.local/share/lampOS-build/rustup
export CARGO_HOME=/home/orangepi/.local/share/lampOS-build/cargo
export PATH="$CARGO_HOME/bin:$PATH"
export CARGO_TARGET_DIR=/ABSOLUTE/FRESH/QUALIFICATION/target
export CARGO_BUILD_JOBS=2
cargo test --locked --offline --workspace -- --test-threads=1
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
cargo build --locked --offline --release -p lamp-live
```

Run sequentially and fix a failed stage before proceeding. ALSA development
headers/pkg-config and libclang 14 are available on the board. Linux UAPI 6.12
headers and `.cargo/config.toml` are part of the standalone source. Do not remove
them when extracting. Host parallel fixture tests have shown load-sensitive
diagnostic finish timeouts; isolated/serial runs passed. Retain that limitation,
not a blanket claim that every host execution was green.

When continuation is authorized, first inspect readiness, thermal state (all
relevant zones <=70 C), current mixer settings, physical privacy state and motor
inhibit. The fixture runner logs temperature but does not enforce that threshold;
the operator must enforce it before execution. It stops installed services,
performs a finite run, cleans up its workers, restores installed services and
checks the stationary safeguard. Review its receipts even after failures.

Example of the existing **future** regression, not a run performed at handoff:

```sh
python3 /Users/autonomous/.local/share/lampOS-handoff/2026-10-10/tools/fixed-reply-harness/run_echo_fixture.py \
  /Users/autonomous/.local/share/lampOS-handoff/2026-10-10/qualification/shutdown-36566770 \
  --reply /home/orangepi/.local/share/lampOS-build/reply-fixture-76x62xf9/reply.wav \
  --reply-sha256 d5bd0d6f880434b81846accf5181b9bf5ab28ac91479827aa9f73ddf8b52e144 \
  --player /Users/autonomous/.local/share/lampOS-handoff/2026-10-10/bin/lamp-observer \
  --stimulus /Users/autonomous/.local/share/lampOS-handoff/2026-10-10/trials/h4jds57i/stimulus.wav \
  --noise-suppression on --seconds 30 --execute
```

Preparation without `--execute` is filesystem-only. Keep finite deadlines and
cleanup/restoration. Do not start unattended motor work or indefinite audio loops.
Do not use a fleet/device-profile OTA for testing. No production flags were
promoted by the latest NS/delay experiments.

## Suggested continuation order

1. Verify current device state and run a bounded physical shutdown regression for
   the already-qualified terminal-close code. Both capture/render certification,
   runtime exit, full delivery, cleanup and restoration must pass. A failure
   remains a failure even if the answer sounded complete.
2. Improve playback-aware turn admission using measured near-end/echo evidence.
   Preserve first words and quiet overlap; do not merely raise VAD threshold,
   mute during playback, add a long cooldown or optimize one cached sentence.
   Compare echo-only false admissions against genuine speech during playback,
   soft interruptions, short follow-ups, hesitations and different topics.
3. Establish a balanced repeated live V1-main/V2 cohort with the same provider
   model/voice/thinking/language, volume, placement and scenario assets. Measure
   final user sound to first substantive audible answer, plus acoustic stop,
   missed/false interruptions, dropped answers, answer correctness and CPU/RSS.
   Include failures; separate direct-human and speaker-replay results.
4. Finish actual addressee/multi-person/call/TV restraint and vision evidence.
   Ring phase cues are connected as an explicit qualification option, with no
   physical rerun. Full camera/sensor/motor choreography is not connected to the
   directed voice runtime yet. Camera V4L2 backend and finite `camera-inspect` are compiled
   but not physically qualified. The motor crate is read-only protocol/calibration
   logic, not a qualified actuator implementation. See component docs before reuse.
5. Qualify desk onboarding, coordinated gestures/light, privacy/failure recovery
   and spoken failures. Movement requires a fresh safe placement review. Then
   ask the owner to judge the real interaction; tests cannot certify "feels like
   talking to a friend" on their behalf.

The existing coordinator/ownership, queue bounds and cancellation primitives are
implemented, but a complete expressive choreographer, social listener and robust
failure experience are not. Avoid calling V2 finished based on test count alone.

## Handoff stop state

All delegated workers finished. No physical experiment, cloud conversation or
posture loop is left running by this handoff. The final native qualification and
device receipt above are the wrap-up checks. Codex pauses the ongoing goal at the
owner's request; it does not mark the product goal complete. Continue from this
evidence rather than regenerating fixtures or repeating completed audits.
