# Automated voice acceptance runner (`lamp-voice-eval`)

Status 2026-10-11 (voice-evaluation workstream, Agent 2): the runner, evaluator,
reports, evaluator self-test, fake runtime and trace import are implemented and
tested offline. The physical runner is implemented and exercised end to end
against a fake `lamp-live` over its real relay and cue socket, but **has not run
on a Lamp or played any audio**. There are no physical attempts yet, so there is
**no positive V2 overlap/interruption cohort**; none may be derived from the fake
runtime. Ranked, reproducible defects are in [voice defects](voice-eval-defects.md).

`crates/voice-eval` turns the [comparison protocol](comparison-protocol.md) into
a repeatable runner: pre-registered scenarios, stimuli started from actual
runtime/playback events with bounded deadlines, an append-only attempt ledger,
and reports that keep software, simulated and acoustic evidence apart. It reuses
`lamp-acoustic` (catalog, verified cache, rendering), `lamp-observer` (iMac
playback; room recording through the signed bundle), `lamp-live` (`TurnDetector`,
VAD, `CueSink`, trace vocabulary) and `lamp-ipc` (clock).

## One command per suite

```sh
cargo run -p lamp-voice-eval -- fake-run --out NEW_DIR          # offline, every scenario
cargo run -p lamp-voice-eval -- physical-run --config FILE --cache .cache/acoustic --manifest M... --out NEW_DIR [--execute]
```

Both first run the evaluator self-test, then every selected attempt, and write
`NEW_DIR/evaluations/<unix-ms>/report.md`, `evaluation.json` and one evidence
page per attempt in `attempts/`. Exit code 2 means the self-test failed and the
report is marked UNTRUSTED; a physical run refuses to start in that case.

## Evidence strata and stimulus sources

Every attempt has one stratum and one stimulus source; reports group by both and
never pool groups.

| Stratum | What produced it | What it can show |
|---|---|---|
| `fake_timing` | Fake runtime, declared speech intervals | Turn-policy consequences: false/missed interruptions, splits, unwanted responses, provider-fault handling, voice/ring state. Not level- or acoustics-sensitive. |
| `fake_signal` | Fake runtime, lamp-live VAD on the exact cached digital mixes | The same, plus what the VAD/detector does with the real stimulus waveform. No room, loudspeaker, microphone or AEC; host VAD numerics can differ from native ARM64. |
| `physical_fixture` | `lamp-live directed-fixture` on the Lamp | Real capture/echo path with the cached Gemini reply. Only the first turn has a reply. |
| `physical_gemini` | `lamp-live directed` | Conversation with event-triggered follow-ups and interruptions through the optional cue socket. |
| `imported_trace` | An existing lamp-live `events.jsonl` | Retained trials scored with declared turn attribution. |

| Source | Meaning |
|---|---|
| `declared_timing`, `digital_mix` | Fake runtime; nothing audible. |
| `loudspeaker_synthetic` | Cached synthetic voices on the explicit iMac speakers. |
| `direct_human` | A person speaks lines prompted silently on the operator console at the trigger. Only the prompt time is known in software. |

Jieli's onboard processing may treat loudspeaker replay and a person
differently, so loudspeaker results never stand in for direct-human ones.
Software timestamps (runtime host clock, ALSA acceptance/retirement, runner
start requests) are never acoustic boundaries. Several synthetic voices from one
loudspeaker cannot establish spatial speaker discrimination.

## Scenario plan and stimuli

`fixtures/voice-eval-v1.json` is the pre-registered plan: 42 scenarios with
expected behavior per step, triggers, deadlines, fake replies/faults and three
fake profiles. `fixtures/voice-eval-stimuli-v1.json` extends `desk-v1` with
eight utterances and seventeen scenes. `desk-v1.json` is unchanged (SHA-256
`636a5199…`); extension utterances that reuse a desk ID must be identical and
scene IDs may not collide. Eight of the new scenes only remix cached speech (for
example `quiet-question` is `math` at -24 dB, `interrupt-sky` reuses `mid-name`);
nine use the eight new utterances, which need speech rendering once, on the Mac
that holds the existing cache (`render-stimuli`, which reuses every verified
object).

| Category | Scenarios |
|---|---|
| Quick questions, rapid follow-ups | `quick-chat`, `quick-fact`, `follow-up-chain` (two follow-ups, each 0.5–0.6 s after the previous answer ends), `rapid-follow-up` (150 ms after the answer ends) |
| Short answers to Lamp's questions | `short-yes`, `short-no` |
| Long answers, thoughtful pauses, unfinished questions | `long-answer`, `hesitant-sharing`, `unfinished-question-pause` (1.4 s pause mid-question), `unfinished-question-resume` (finished 300 ms into Lamp's reply to the fragment) |
| Natural topic changes, repeated interruptions | `topic-change` (+1.2 s), `topic-change-late` (+4 s), `repeated-interruptions` (three in a row), `fixed-reply-topic-change` |
| Correction, quiet and rapid speech | `self-correction`, `quiet-question`, `rapid-question` (240 wpm) |
| Listener acknowledgments | `listener-acknowledgment`, `ack-yeah`, `ack-right`, `fixed-reply-acknowledgment` |
| Background conversation, two speakers, other devices, noise | `background-conversation-question`, `two-colleagues`, `ambiguous-not-invited`, `overlapping-speakers`, `colleague-aside`, `other-device`, `computer-call`, `background-media`, `noise-only`, `noisy-question`, `typing-question` |
| Echo-only playback | `fixed-reply-echo-only` (the h4jds57i condition), `long-answer` |
| Delays, disconnects and recovery (fake only) | `provider-slow-first-audio`, `provider-late-answer`, `provider-supply-gap`, `slow-response-then-follow-up`, `provider-failure-before-audio`, `provider-disconnect-mid-reply`, `disconnect-between-turns`, `disconnect-mid-reply-recovery`, `provider-spurious-interrupt` |

Expectations: `answer` (one turn, complete within the 15 s deadline),
`interrupt_and_answer`, `no_interrupt`, `silence`, `observe` (no admission during
Lamp's own playback), `honest_failure` (explicit failure, no fabricated
completion, spoken notice), `recover_and_answer` (the session survives an
injected provider failure and answers) and `fragment` (an unfinished utterance,
judged through the step that continues it). A step in a multi-talker scene can
list its `addressed` utterances; admissions whose captured input never overlaps
them are background responses. Validation rejects unknown scenes or addressed
utterances, unbounded deadlines, overlap expectations without the speaking
precondition, plan triggers that disagree with a scene's declared trigger, and
fault injection claimed as physical.

## Event triggering and retention

The first step follows `listening_ready`; later steps follow
`speaker_first_write`, `speech_retired` or `turn_cancelled` of a turn newer than
every turn known when the previous step started, then wait the declared delay.
Waiting is bounded by the step deadline. A missing event gives `trigger_missed`;
an ended session `session_ended`; a planned start that passed more than 100 ms
before the trigger could act `trigger_stale`; an overlap step whose reply stopped
before injection `precondition_lost`. Nothing is played in those cases and later
steps are withheld. A player reporting failed delivery marks the step
`delivery_failed`. There is no fixed-delay fallback. Stimulus timing (which
reads and verifies cached audio) is computed before the session starts.

The ledger (`RUN/ledger.jsonl`, mode 0600, append-only, sequence-checked) holds
`run_start` (plan/catalog hashes, backend, seed, attempt order, evaluator
self-test), `attempt_planned` (synced before any session or stimulus),
`attempt_finished` (step receipts, live events, authoritative events, clock map,
evidence) and `run_end`. Withheld and unsupported attempts are retained with
their reason. Evaluation reports planned-but-unfinished attempts and a truncated
final line. Scoring can be repeated on an old ledger with `evaluate`.

A physical attempt is **invalid** (listed, but excluded from every rate and
latency) when its session transport closes before `session_end`, the relay
reports an error or hard-deadline kill, or fewer trace lines arrive than the
relay sent. A trace must contain both `run_start` and `run_end` to be scored.

## Evaluation rules

Admissions are attributed to the stimulus whose speech window contains them,
after mapping step times into the event clock (identity for the fake runtime;
ping/pong over the session transport for physical runs, widened by its
uncertainty and a playback-path allowance of 250 ms for loudspeaker replay).
A scene without speech owns its whole duration. Direct-human prompts have no
verified speech window: timed direct-human attempts remain unscored, with raw
records retained, until speech and turn attribution can be reviewed. A prompt
being displayed does not establish speech delivery, silence, or an interruption.
See [acceptance boundaries](voice-eval-acceptance-boundaries.md).
Imported traces use declared `step=turn` pairs; every stimulus step must be
declared (`--turn STEP=none` when it produced no admission), except that a
single-stimulus scenario defaults to the first admission.

A `user_interrupted` cancellation is caused by the admission whose speech
candidate is within 20 ms of it. A planned `interrupt_and_answer` step yields as
planned; anything else, including background talk, is a **false interruption**.
A provider interruption without planned speech is also false. A reply whose
final sample retired before a later input revoked it counts as complete. Overlap
steps whose stimulus started after the reply had ended are unscored
(`overlap_not_achieved`), and a start more than 250 ms late is flagged
`late_stimulus`. An injected fault pre-empted by another cancellation is
unscored, not failed.

Answers are complete, complete with playback gaps, yielded as planned, late
(right-censored), **truncated** (audio started, not finished), **missing** (no
audio for an admitted request, or never admitted), or unsupported (fixture
second turns). One request answered aloud twice is a **duplicate answer**.
Other findings: missed interruption, unwanted response, turn split, lost opening
words (exact in the fake runtime; physical needs proposal P5), possible lost
opening words (provider transcript hypothesis only), runtime failure, no
recovery, unannounced failure and fabricated completion.

**Voice and ring state through cancellation** is checked on every trace: no
speaker output for a turn after it was revoked, never two replies playing at
once, every admitted turn terminated in a completed run, and, when the runtime
records ring requests (`--ring-channel-ceiling`), no cue for an ended turn and
each listening/waiting/speaking cue consistent with that turn's voice state
within 50 ms. Attempts without ring requests are counted as ring-unchecked.

Latency rows are labeled `simulated` (fake profile; restates configured delays),
`software` (lamp-live host clock), `runner` (start-request lateness) or
`ACOUSTIC` (annotations). Release targets apply only to ACOUSTIC rows.
Percentiles use nearest rank with the exact n. Rates show numerators and
denominators; answers yielded to a planned interruption and answers the
provider cannot give (fixture second turns) are listed outside the
complete-answer denominator. Completion credit now also excludes confirmed
answer-content and duplicate/split failures while retaining those opportunities.
Reports separate raw completed playback, positively reviewed completions and
unreviewed completions; see [answer review accounting](voice-eval-answer-review.md).
An unreviewed completed playback does not establish semantic success.
Both acoustic boundaries, final
user speech to first substantive audible word and interrupting speech to audible
silence, always appear with measured and unmeasured counts and the reasons.

Annotations (`annotation-template` creates the file) must name the room WAV
SHA-256, set `listened: true` after a person listened, and give both boundaries
in WAV seconds. Hints from runner times locate stimuli but are never scored. A
missing, unlistened, mismatched, one-sided or negative annotation stays
unscored. Reviewer judgments in a usable annotation count: `answer_complete:
false` and `answer_relevant: false` fail the step, and `spoken_failure_notice`
scores physical honest-failure steps. The actual WAV is now opened, structurally
checked and hashed at import/collection and again at scoring. Run identity,
finite in-recording boundaries and a successful supervised recorder exit are
required where applicable. Missing or changed files remain visible and unscored.
See [recording evidence](voice-eval-recording-evidence.md) for bounds, supported
formats, old-ledger migration and the limits of these checks.

## Evaluator self-test

The self-test uses its own embedded canonical plan and catalog, independent of
the run being evaluated. Results retain their versions and content hashes;
[canonical canary identity](voice-eval-canary-plan.md) describes re-evaluation
of historical or custom plans.

`self-test` runs 21 canaries before any report is trusted. Three clean controls
must pass (a conversation, kept silence, a planned interruption). Eighteen
injected failures must each be detected and fail the attempt, injected either
through the fake runtime's own faults (echo leak, acknowledgment, unaddressed
talk, split, supply gap, late answer, silent failure, no recovery) or by
mutating a clean attempt's events (removed interruption, missing, truncated or
duplicate answer, dropped opening words, stale output after revocation, a ring
cue ahead of the voice, overlapping replies, a partial trace, and a physical
attempt with room audio but no annotation, which must stay unmeasured).

## Other commands

From `lampOS/`:

```sh
cargo run -p lamp-voice-eval -- validate | list | self-test
cargo run -p lamp-voice-eval -- fake-run --out NEW_DIR [--profile v2-directed-current|v2-echo-leak-h4|v2-echo-loop] [--scenario ID]... [--repetitions N] [--seed N] [--attempt-seed N]
cargo run -p lamp-voice-eval -- fake-run --out NEW_DIR --cache .cache/acoustic --manifest .cache/render-first.json   # fake_signal
cargo run -p lamp-voice-eval -- evaluate RUN_DIR [--annotations FILE]
cargo run -p lamp-voice-eval -- import-trace --events artifacts/.../events.jsonl --scenario fixed-reply-echo-only --out NEW_DIR [--turn greet=1] [--source loudspeaker_synthetic|direct_human] [--room-metadata room/metadata.json]
cargo run -p lamp-voice-eval -- annotation-template RUN_DIR NEW_FILE.json
cargo run -p lamp-voice-eval -- assets --cache .cache/acoustic --manifest MANIFEST...
cargo run -p lamp-voice-eval -- render-stimuli .cache/acoustic NEW_REPORT.json   # macOS; renders only missing objects
```

Every failed attempt carries a reproduction command; fake attempts reproduce
exactly with `--attempt-seed`.

## Physical runner (prepared, not executed)

When Lamp returns, the operator first performs the readiness checks in the
[handoff](../HANDOFF.md): thermal state, mixer readings, physical privacy,
motor inhibit, exclusive ownership (stop installed services) and a rollback
plan. `lamp-voice-eval` does not stop or restore services, change mixers, move
motors or open SSH by itself; `lamp-live` still refuses to run while legacy
owners are active, and such an attempt is retained as failed.

1. Build natively on the Lamp with the pinned toolchain:
   `cargo build --locked --offline --release -p lamp-voice-eval -p lamp-live`.
2. Render the eight new utterances once on the Mac that holds the existing
   cache, then check that `assets` resolves every scene to be run.
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
  "ring_channel_ceiling": 24,
  "stimulus_source": "loudspeaker_synthetic",
  "output_device": "iMac Speakers",
  "room_recorder": ["open", "-n", "-W", "-a", "/private/tmp/LampRoomObserver.app", "--args",
    "record", "--input", "EXACT INPUT NAME", "--seconds", "{seconds}", "--out", "{out}"],
  "room_independent": true
}
```

   Use `provider_config` (a private provider JSON on the Lamp) instead of the
   fixture pair for Gemini. For a direct-human cohort set `"stimulus_source":
   "direct_human"` and `"human_speaker": "person-1"` (a label, not a name); the
   console then shows each line to say at its trigger and nothing is played.
   Prompt-only timed attempts stay unscored until reviewed attribution is supplied.
   Without `room_recorder` every acoustic boundary is unmeasured. Set
   `room_independent` to false when the recorder shares Lamp's hardware (for
   example the C-Media ambient input). Omit `ring_channel_ceiling` to run
   without the ring worker.
4. Dry run (the default) prints which attempts would run or be withheld:
   `cargo run --release -p lamp-voice-eval -- physical-run --config FILE --cache .cache/acoustic --manifest M... --out NEW_DIR --scenario fixed-reply-echo-only`
5. Add `--execute` for the authorized run. Each attempt starts one finite
   `lamp-session` (fresh private work directory, cue socket, `lamp-live`, hard
   kill at `seconds + allowance`), maps clocks by ping/pong during the session,
   starts the room recorder and waits for its `ready.json`, starts each stimulus
   on its trigger, joins playback within a bound (cancelling through the
   sentinel file if needed), and imports the relayed `events.jsonl`. Then fill
   the annotation template and run `evaluate`.

With the fixture provider the ready scenarios are `fixed-reply-echo-only`,
`fixed-reply-acknowledgment` and `fixed-reply-topic-change`. Gemini sessions now
receive the same lifecycle cues, including admissions and local endpoints, so
follow-up and interruption scenarios can use actual playback events. Missing,
stale or invalid cues cannot be replaced by guessed delays. Provider-specific
and fault-injection restrictions still apply. See [directed cue evaluation](voice-eval-directed-cues.md).
An admission cue reports the runtime's decision; it does not prove that a human
addressed Lamp or that echo was rejected.

## Measured behavior of the runner itself

On the local loopback (debug build, one host, fake lamp-live over the real relay
and cue socket), intended stimulus start to runner start request was 166.5 ms
(n=1) when stimulus timing was computed after the trigger, and 2.6/3.8/4.7 ms
(n=3) after moving it before the session. Clock-map uncertainty was about
±1.1 ms. These are runner software boundaries on one host; real SSH,
`lamp-observer` stream preparation and acoustic output add delay that the player
report and room audio must measure.

## Current offline results (simulation only)

The table below retains Agent 2's reported simulation baseline through
`3eaa0365a`. It has not been rerun with the integrated answer-review and
recording-provenance fixes and does not qualify the combined source.

`fake-run --repetitions 3` (seed 1, 126 attempts per profile, self-test 21/21)
against the lamp-live directed policy at 23bde487. These describe what the
current turn policy does with declared speech; they are not Lamp measurements.

| Profile | Outcome | Main findings |
|---|---|---|
| `v2-directed-current` (perfect echo cancellation) | 60 passed, 60 failed, 6 incomplete | 90/120 complete answers; acknowledgments and asides interrupt 15/15; 15/18 expected-silence steps admit unaddressed speech; 6 turn splits from pauses; 6 sessions end instead of recovering; 9 silent provider failures; 0/21 missed interruptions; no voice/ring inconsistency in 123 ring-checked attempts. |
| `v2-echo-leak-h4` (one residual-echo burst) | 3 passed, 120 failed, 3 incomplete | Complete answers fall to 33/123; 33 overlap steps are withheld because the answer was already cancelled. |
| `v2-echo-loop` (a burst after every reply) | 3 passed, 123 failed | Self-sustaining admission chains as in zejfwxq7: 0/102 complete answers. |

Timing mode is level-blind: quiet, rapid and noisy-question scenarios pass by
construction there; `fake_signal` with the real cache or physical runs are
needed for them. See [voice defects](voice-eval-defects.md) for the ranked list,
owners and exact reproduction commands.

## Runtime integration and remaining proposals

- **P1 implemented:** Gemini-directed sessions now use [conversation lifecycle cues](live-session-cues.md),
  including admissions, endpoints and original cancellation reasons. Host producer
  and relay checks do not substitute for ARM64 or real Lamp qualification.
- **P2: utterance timing in render manifests.** Adding each utterance's cache key
  and measured active-speech span to `RenderReport` would replace text/energy
  estimates of clip boundaries.
- **P3: fault injection for physical provider tests.** Optional, test-only
  provider delays, stalls and disconnects for the recovery and delay scenarios.
- **P4: armed playback in `lamp-observer`.** A prepare/open-then-start API and a
  report of the first callback carrying nonzero samples would shorten and
  measure trigger-to-sound latency on the iMac.
- **P5: opening-word retention from capture diagnostics.** Align the stimulus in
  `pre_aec.pcm16le` with `prefix_first_host_read_us` in the same Lamp clock.
- **P6: a trace event for spoken failure notices**, once the runtime has one.
