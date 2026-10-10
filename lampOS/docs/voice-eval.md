# Automated voice acceptance runner (`lamp-voice-eval`)

Status 2026-10-10: the runner, evaluator, report, fake runtime and trace import
are implemented and tested offline. The physical runner is implemented and
exercised end to end against a fake `lamp-live` over its real relay and cue
socket, but **has not run on a Lamp or played any audio**. There are no
physical attempts yet, so there is **no positive V2 overlap/interruption
cohort**; none may be derived from the fake runtime.

`crates/voice-eval` turns the [comparison protocol](comparison-protocol.md) into
a repeatable runner: pre-registered scenarios, stimuli started from actual
runtime/playback events with bounded deadlines, an append-only attempt ledger,
and a report that keeps software, simulated and acoustic evidence apart. It
reuses `lamp-acoustic` (catalog, verified cache, rendering), `lamp-observer`
(iMac playback; room recording through the signed bundle), `lamp-live`
(`TurnDetector`, VAD, `CueSink`, trace vocabulary) and `lamp-ipc` (clock).

## Evidence strata

Each attempt belongs to exactly one stratum; reports never pool them.

| Stratum | What produced it | What it can show |
|---|---|---|
| `fake_timing` | Fake runtime, declared speech intervals | Turn-policy consequences: false/missed interruptions, splits, unexpected responses, provider-fault handling. Not level- or acoustics-sensitive. |
| `fake_signal` | Fake runtime, lamp-live VAD on the exact cached digital mixes | The same, plus what the VAD/detector does with the real stimulus waveform. No room, loudspeaker, microphone or AEC; host VAD numerics can differ from native ARM64. |
| `physical_fixture` | `lamp-live directed-fixture` on the Lamp, iMac loudspeaker | Real capture/echo path with the cached Gemini reply. Only the first turn has a reply. |
| `physical_gemini` | `lamp-live directed`, iMac loudspeaker | Real conversation. Event-triggered steps need proposal P1. |
| `imported_trace` | An existing lamp-live `events.jsonl` | Retained trials scored with declared turn attribution. |

Software timestamps (runtime host clock, ALSA acceptance/retirement, runner
start requests) are never acoustic boundaries. Acoustic latency exists only
from reviewed annotations of a continuous room recording; anything else is
reported as unscored with its reason. Several synthetic voices from one iMac
speaker cannot establish spatial speaker discrimination; the report says so
whenever a multi-voice scene is present.

## Scenario plan and stimuli

`fixtures/voice-eval-v1.json` is the pre-registered plan: 34 scenarios,
expected behavior per step, triggers, deadlines, fake replies/faults and fake
profiles. `fixtures/voice-eval-stimuli-v1.json` extends `desk-v1` with six
utterances and nine scenes. `desk-v1.json` is unchanged (SHA-256 `636a5199…`);
extension utterances that reuse a desk ID must be identical, and scene IDs may
not collide. Three of the nine new scenes only remix cached speech
(`quiet-question` is `math` at -24 dB, `topic-change-late` has the same mix key
as `topic-change`, `colleague-aside` reuses `colleague-overlap`); the other six
use the new utterances. Only those six utterances need speech rendering, once,
on the Mac that holds the existing cache (`render-stimuli`, which reuses every
verified object).

| Brief category | Scenarios |
|---|---|
| Quick chat, repeated follow-ups | `quick-chat`, `quick-fact`, `follow-up-chain` (two follow-ups, each triggered by the end of the previous answer) |
| Short answers to Lamp's questions | `short-yes`, `short-no` (Lamp is asked to pose a yes/no question first) |
| Long answers, topic-change interruptions | `long-answer`, `topic-change` (+1.2 s), `topic-change-late` (+4 s), `fixed-reply-topic-change` |
| Hesitation, correction, quiet, rapid | `hesitant-sharing`, `self-correction`, `quiet-question`, `rapid-question` (240 wpm) |
| Listener acknowledgments | `listener-acknowledgment`, `ack-yeah`, `ack-right`, `fixed-reply-acknowledgment` |
| Two people, computer audio, noise | `two-colleagues`, `ambiguous-not-invited`, `overlapping-speakers`, `colleague-aside`, `other-device`, `computer-call`, `background-media`, `noise-only`, `noisy-question`, `typing-question` |
| Echo-only playback | `fixed-reply-echo-only` (the h4jds57i condition), `long-answer` |
| Provider delay, failure, disconnect | `provider-slow-first-audio`, `provider-late-answer`, `provider-supply-gap`, `provider-failure-before-audio`, `provider-disconnect-mid-reply`, `provider-spurious-interrupt` (fake only) |

Expectations: `answer` (one turn, complete answer within the 15 s deadline),
`interrupt_and_answer`, `no_interrupt`, `silence`, `observe` (no admission
during Lamp's own playback) and `honest_failure` (explicit failure outcome, no
fabricated completion, spoken notice). Validation rejects unknown scenes,
unbounded deadlines, overlap expectations without the speaking precondition,
plan triggers that disagree with a scene's declared trigger, and fault
injection claimed as physical.

## Event triggering and retention

The first step follows `listening_ready`; later steps follow
`speaker_first_write` or `speech_retired` of a turn newer than every turn known
when the previous step started, then wait the declared delay. Waiting for the
event is bounded by the step deadline. If the event does not arrive, the step is
`trigger_missed`; if the session ends first, `session_ended`; if the planned
start passed more than 100 ms before the trigger could act (a late cue),
`trigger_stale`; if an overlap step finds the triggering reply no longer playing
at injection time, `precondition_lost`. In each case nothing is played and later
steps are withheld. A player that reports failed delivery marks the step
`delivery_failed`. There is no fixed-delay fallback. Stimulus timing (which
reads and verifies cached audio) is computed before the session starts so it
never sits between a trigger and its stimulus.

The ledger (`RUN/ledger.jsonl`, mode 0600, append-only, sequence-checked) holds
`run_start` (plan/catalog hashes, backend, seed, attempt order),
`attempt_planned` (synced before any session or stimulus), `attempt_finished`
(complete record: step receipts, live events, authoritative events, clock map,
evidence) and `run_end`. Withheld and unsupported attempts are retained with
their reason. Evaluation reports planned-but-unfinished attempts and a truncated
final line instead of dropping them. Scoring can be repeated on an old ledger.

A physical attempt is **invalid** (excluded from every rate and latency, but
listed) when its session transport closes before `session_end`, the relay
reports an error or a hard-deadline kill, or fewer trace lines arrive than the
relay sent. A trace must contain both `run_start` and `run_end` to be scored.

## Evaluation rules

Admissions are attributed to the stimulus whose speech window contains them,
after mapping step times into the event clock (identity for the fake runtime;
ping/pong over the session transport for physical runs, widened by its
uncertainty and a 250 ms playback-path allowance). A scene without speech (noise
only) owns its whole duration. Imported traces use declared `step=turn` pairs;
every stimulus step must be declared (`--turn STEP=none` when it produced no
admission), except that a single-stimulus scenario defaults to the first
admission. Unattributed admissions are echo, noise or unplanned sound.
A `user_interrupted` cancellation is caused by the admission within 20 ms of it:
a planned `interrupt_and_answer` step yields as planned; anything else is a
**false interruption**. A provider interruption without planned speech is also
false. A reply whose final sample retired before a later input revoked it (the
runtime waits for provider idle too) counts as complete, not interrupted.
Overlap steps whose stimulus started after the reply had ended are unscored
(`overlap_not_achieved`), and a start more than 250 ms after plan is flagged
`late_stimulus`. Findings: false/missed interruption, unexpected response,
missed input, turn split, incomplete/late/no answer, lost opening words (exact in the fake
runtime; physical needs proposal P5), possible lost opening words (provider
transcript hypothesis only), playback gaps (defect), runtime failure,
unannounced failure, fabricated completion, and withheld reasons. An injected
fault pre-empted by another cancellation is unscored, not failed.

Latency rows are labeled `simulated` (fake profile; restates configured delays),
`software` (lamp-live host clock), `runner` (start-request lateness) or
`ACOUSTIC` (annotations). Release targets apply only to ACOUSTIC rows.
Percentiles use nearest rank with the exact n. Rates show numerators and
denominators; answers yielded to a planned interruption and answers the
provider cannot give (fixture second turns) are listed outside the
complete-answer denominator.

Annotations (`annotation-template` creates the file) must name the room WAV
SHA-256, set `listened: true` after a person listened, and give both boundaries
in WAV seconds. Hints from runner times locate stimuli but are never scored. A
missing, unlistened, mismatched, one-sided or negative annotation stays
unscored. Reviewer judgments in a usable annotation count: `answer_complete:
false` and `answer_relevant: false` fail the step, and `spoken_failure_notice`
scores physical honest-failure steps.

## Commands

From `lampOS/`:

```sh
cargo run -p lamp-voice-eval -- validate
cargo run -p lamp-voice-eval -- list
cargo run -p lamp-voice-eval -- fake-run --out NEW_DIR [--profile v2-directed-current|v2-echo-leak-h4|v2-echo-loop] [--scenario ID]... [--repetitions N] [--seed N]
cargo run -p lamp-voice-eval -- fake-run --out NEW_DIR --cache .cache/acoustic --manifest .cache/render-first.json   # fake_signal
cargo run -p lamp-voice-eval -- evaluate RUN_DIR [--annotations FILE]
cargo run -p lamp-voice-eval -- import-trace --events artifacts/.../events.jsonl --scenario fixed-reply-echo-only --out NEW_DIR [--turn greet=1] [--room-metadata room/metadata.json]
cargo run -p lamp-voice-eval -- annotation-template RUN_DIR NEW_FILE.json
cargo run -p lamp-voice-eval -- assets --cache .cache/acoustic --manifest MANIFEST...
cargo run -p lamp-voice-eval -- render-stimuli .cache/acoustic NEW_REPORT.json   # macOS; renders only missing objects
```

Each run writes `RUN/evaluations/<unix-ms>/report.md` and `evaluation.json`.
Every failed attempt in the report carries a reproduction command; fake attempts
reproduce exactly with `--attempt-seed`.

## Physical runner (prepared, not executed)

When Lamp returns, the operator first performs the readiness checks in the
[handoff](../HANDOFF.md): thermal state, mixer readings, physical privacy,
motor inhibit, exclusive ownership (stop installed services) and a rollback
plan. `lamp-voice-eval` does not stop or restore services, change mixers, move
motors or open SSH by itself; `lamp-live` still refuses to run while legacy
owners are active, and such an attempt is retained as failed.

1. Build natively on the Lamp with the pinned toolchain and copy nothing from
   sibling projects: `cargo build --locked --offline --release -p lamp-voice-eval -p lamp-live`.
2. Render the six new utterances once on the Mac that holds the existing cache,
   then check `assets` resolves every scene to be run.
3. Write a private config (no credentials; it reuses the authorized SSH setup):

```json
{
  "lamp_command": ["/usr/bin/ssh", "-S", "/private/tmp/lamp-4ace-voice-ssh", "-o", "BatchMode=yes",
    "orangepi@172.168.20.159", "/home/orangepi/.local/share/lampOS-build/BUILD/lamp-voice-eval",
    "lamp-session", "--runtime", "/home/orangepi/.local/share/lampOS-build/BUILD/lamp-live"],
  "lamp_work_root": "/home/orangepi/.local/share/lampOS-build/voice-eval-runs",
  "fixture_reply": "/home/orangepi/.local/share/lampOS-build/reply-fixture-76x62xf9/reply.wav",
  "fixture_sha256": "d5bd0d6f880434b81846accf5181b9bf5ab28ac91479827aa9f73ddf8b52e144",
  "noise_suppression": "on",
  "output_device": "iMac Speakers",
  "room_recorder": ["open", "-n", "-W", "-a", "/private/tmp/LampRoomObserver.app", "--args",
    "record", "--input", "EXACT INPUT NAME", "--seconds", "{seconds}", "--out", "{out}"],
  "room_independent": true
}
```

   Use `provider_config` (a private provider JSON on the Lamp) instead of the
   fixture pair for Gemini. Without `room_recorder` every acoustic measure is
   unscored. Set `room_independent` to false when the recorder shares Lamp's
   hardware (for example the C-Media ambient input).
4. Dry run (the default) prints which attempts would run or be withheld:
   `cargo run --release -p lamp-voice-eval -- physical-run --config FILE --cache .cache/acoustic --manifest M... --out NEW_DIR --scenario fixed-reply-echo-only`
5. Add `--execute` for the authorized run. Each attempt starts one finite
   `lamp-session` (fresh private work directory, cue socket, `lamp-live`, hard
   kill at `seconds + allowance`), maps the clocks by ping/pong during the
   session, starts the room recorder and waits for its `ready.json`, plays
   each stimulus on its trigger through `lamp-observer`, joins playback within a
   bound (cancelling through the sentinel file if needed), and imports the
   relayed `events.jsonl`. Then fill the annotation template and run `evaluate`.

With the fixture provider, the ready scenarios are `fixed-reply-echo-only`,
`fixed-reply-acknowledgment` and `fixed-reply-topic-change`. With Gemini, only
scenarios whose single step follows readiness can run until P1 lands; the rest
are withheld with that reason. Cues carry no admissions, so a reply-triggered
step binds to the first reply newer than every turn seen in cues; an echo turn
admitted in between can be bound instead (the evaluator still reports the false
interruption). P1's `input_admitted` cue removes that ambiguity.

## Measured behavior of the runner itself

On the local loopback (debug build, one host, fake lamp-live over the real relay
and cue socket), intended stimulus start to runner start request was
166.5 ms (n=1) when stimulus timing was computed after the trigger, and
2.6/3.8/4.7 ms (n=3) after moving it before the session. Clock-map uncertainty
was about ±1.1 ms. These are runner software boundaries on one host; real SSH,
`lamp-observer` stream preparation and acoustic output add delay that the player
report and room audio must measure.

## Current offline results (simulation only)

`fake-run --repetitions 3` (seed 1, 102 attempts per profile) against the
lamp-live directed policy at 64529dee. These describe what the current turn
policy does with declared speech; they are not Lamp measurements.

| Profile | Outcome | Main findings |
|---|---|---|
| `v2-directed-current` (perfect echo cancellation) | 51 passed, 48 failed, 3 incomplete | Every acknowledgment and the colleague aside cancel Lamp (15/18 acknowledgment/echo windows; the 3 echo-only windows pass). 15 of 18 expected-silence steps admit unaddressed speech, mostly answered aloud (two colleagues, other device, call, media, unaddressed question). The hesitant thought's internal pause (about 0.8 s by text estimate) exceeds the 600 ms endpoint and splits it into two turns. Provider failures and disconnects end with no spoken notice. Topic changes yield and are answered (0/9 missed). |
| `v2-echo-leak-h4` (one residual-echo burst) | 3 passed, 99 failed | The burst is admitted about 0.9 s after the first write in every attempt that speaks, reproducing h4jds57i; complete answers fall to 15/90; 24 overlap steps are withheld because the answer was already cancelled. |
| `v2-echo-loop` (a burst after every reply) | 3 passed, 99 failed | Self-sustaining admission chains as in zejfwxq7: 0/78 complete answers. |

Timing mode is level-blind: quiet, rapid and noisy-question scenarios pass
there by construction; `fake_signal` with the real cache or physical runs are
needed for them. A signal-mode probe found that lamp-live's VAD scores a steady
200 Hz tone at about 0.97 speech probability (white noise peaks near 0.77), so
tonal sounds such as beeps may be admitted by the current start rule.

## Proposals outside this crate (not implemented)

These need changes owned by other components; they are proposals only.

- **P1: cues in `directed` mode.** `lamp-live directed` creates no `CueSink`, so
  Gemini runs cannot trigger follow-ups or interruptions from events. Pass the
  existing `--cue-socket` option through `run_directed_with_options`, and add
  `input_admitted` and `local_endpoint` cue kinds (and the cancellation reason)
  to `CueKind`. No other runtime behavior needs to change.
- **P2: utterance timing in render manifests.** `RenderReport` lists scene
  assets only. Adding each utterance's cache key and measured active-speech span
  would replace text/energy estimates of clip boundaries.
- **P3: fault injection for physical provider tests.** Optional, test-only
  fixture-provider delays, supply gaps and failures would let the provider
  scenarios run on hardware.
- **P4: armed playback in `lamp-observer`.** A prepare/open-then-start API and a
  report of the first callback carrying nonzero samples would shorten and
  measure trigger-to-sound latency on the iMac.
- **P5: opening-word retention from capture diagnostics.** Align the stimulus in
  `pre_aec.pcm16le` and compare it with `prefix_first_host_read_us` in the same
  Lamp clock to measure lost opening speech on device.
- **P6: a trace event for spoken failure notices**, once the runtime has one, so
  `honest_failure` can be scored from software rather than only by listening.
