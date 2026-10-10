# Ranked voice defects (voice-evaluation workstream, Agent 2)

Checkpoint 2026-10-11, against `lamp-live` at `23bde487` (candidates before
admission; directed mode still accepts VAD-only activity). Lamp was unavailable,
so every new result below is a **fake-runtime policy reproduction**, not a
physical measurement, unless it cites a retained physical trial. Simulation
counts are 3 repetitions of each scenario (seed 1) under the named profile; they
show what the current turn policy does, not how often a person will meet it.
The evaluator passed its 21-canary self-test (18 injected failures detected,
3 clean controls passed) before every run quoted here.

Ranking is by how directly a person at the desk would notice the defect in a
normal conversation. Owners follow
[development workstreams](development-workstreams.md): **Codex** owns input
admission and `coordinator.rs`; **Agent 1** owns `crates/gemini/**` and
`provider_worker.rs`.

Shared setup for every command (from `lampOS/`):

```sh
cargo build --release --locked -p lamp-voice-eval
alias ve=target/release/lamp-voice-eval
ve self-test            # must print 21 canaries, all "detected": true
```

Each run writes `NEW_DIR/evaluations/<ms>/report.md` with one evidence page per
attempt under `attempts/`, including the exact reproduction command.

## D1. Lamp's own playback is admitted as a new turn and cuts its answer (Codex)

- **Evidence:** physical trials `h4jds57i` (second admission 846.505 ms after the
  first accepted write, cached reply cancelled after 20,400 of 107,760 frames),
  `zejfwxq7` (four admissions from one question), and the failed greeting pilots
  in [benchmark progress](benchmark-progress.md). Simulation reproduces the
  mechanism; it does not estimate its rate.
- **Reproduce offline:** `ve fake-run --profile v2-echo-leak-h4 --scenario fixed-reply-echo-only --out NEW_DIR`
  (false interruption ~0.9 s after the first write). Across all 42 scenarios
  under that profile only 33/123 answers complete. Echo chains:
  `ve fake-run --profile v2-echo-loop --scenario quick-chat --out NEW_DIR`.
- **Score a retained trial:** `ve import-trace --events artifacts/fixed-reply-clock-continuity-20261010/<run>/events.jsonl --scenario fixed-reply-echo-only --out NEW_DIR`.
- **Physical repeat (ready now with the fixture provider):** `fixed-reply-echo-only`,
  see the physical procedure in [voice evaluation](voice-eval.md).
- **Expected:** no admission while only Lamp's reply is audible; the reply completes.

## D2. Listener acknowledgments and asides cancel the answer and get answered (Codex)

- **Evidence:** simulation, 15/15 attempts (`listener-acknowledgment` "Mm-hmm",
  `ack-yeah`, `ack-right`, `colleague-aside`, `fixed-reply-acknowledgment`). Each
  admits ~60 ms after the acknowledgment's onset, revokes the playing answer
  (`user_interrupted`) and then answers the acknowledgment ("Okay.").
- **Reproduce:** `ve fake-run --scenario listener-acknowledgment --scenario ack-yeah --scenario ack-right --scenario colleague-aside --out NEW_DIR`
- **Physical repeat (ready now):** `fixed-reply-acknowledgment`. A direct-human
  cohort is required before judging the fix (`stimulus_source: direct_human`).
- **Expected:** the answer continues; nothing new is admitted. Topic changes must
  still yield: `topic-change`, `topic-change-late` and `repeated-interruptions`
  currently pass (0/21 missed interruptions) and are the regression guard.

## D3. Unaddressed speech is admitted and answered (Codex)

- **Evidence:** simulation, 15 of 18 expected-silence steps fail
  (`two-colleagues`, `ambiguous-not-invited`, `other-device`, `computer-call`,
  `background-media`); only `noise-only` passes. In
  `background-conversation-question` (3/3) a colleague's line is admitted as a
  new turn and revokes the owner's still-pending question, which is never answered.
- **Reproduce:** `ve fake-run --scenario two-colleagues --scenario other-device --scenario computer-call --scenario background-conversation-question --out NEW_DIR`
- **Limit:** several synthetic voices leave one iMac loudspeaker; this cannot
  establish spatial speaker discrimination. Directed mode has no addressee
  classifier by design ([input admission](input-admission.md)); this item records
  the user-visible consequence, not a regression.
- **Expected:** no reply and no social acknowledgment; the owner's addressed
  question is answered despite background talk.

## D4. A pause longer than 600 ms splits one request into two turns (Codex)

- **Evidence:** simulation, 6/6 attempts: `hesitant-sharing` (pause ~0.8 s by
  text estimate) and `unfinished-question-pause` (1.4 s mid-question pause). The
  first part is revoked before any audio and Lamp answers only the tail
  ("longest day?"). `unfinished-question-resume`, where Lamp has already started
  replying to the fragment, yields correctly.
- **Reproduce:** `ve fake-run --scenario hesitant-sharing --scenario unfinished-question-pause --out NEW_DIR`
- **Expected:** one turn spanning the pause, answered as a whole.

## D5. A provider disconnect ends the session; nothing recovers (Agent 1, with Codex)

- **Evidence:** simulation of the current failure path (a provider worker error
  fails the finite run), 6/6 attempts: `disconnect-between-turns` (idle drop 1 s
  after a completed answer) and `disconnect-mid-reply-recovery`. The next
  question can never be asked: `NoRecovery`.
- **Reproduce:** `ve fake-run --scenario disconnect-between-turns --scenario disconnect-mid-reply-recovery --out NEW_DIR`
- **Expected:** bounded reconnect; the following question is answered in the same
  session. Physical repetition needs test-only fault injection (proposal P3).

## D6. Provider failures are silent to the person (Agent 1, with Codex)

- **Evidence:** simulation, 9 attempts with an injected failure
  (`provider-failure-before-audio`, `provider-disconnect-mid-reply`,
  `disconnect-mid-reply-recovery`): the turn ends `runtime_failed` with no spoken
  notice. Matches the documented gap in [live runtime](live-runtime.md).
- **Reproduce:** `ve fake-run --scenario provider-failure-before-audio --scenario provider-disconnect-mid-reply --out NEW_DIR`
- **Expected:** an honest, brief spoken notice; never a fabricated completion
  (none was fabricated).

## D7. A provider interruption cancels a playing answer without local speech (Agent 1)

- **Evidence:** simulation, 3/3 (`provider-spurious-interrupt`): an `interrupted`
  event 1.5 s into playback with no admitted user input revokes the answer.
  With automatic activity detection disabled this should not occur in normal
  operation; the coordinator does not check that a local `activityStart` preceded it.
- **Reproduce:** `ve fake-run --scenario provider-spurious-interrupt --out NEW_DIR`
- **Expected:** an interruption is honored only when it follows local input.

## D8. Slow provider delivery has no user-facing handling (Agent 1)

- **Evidence:** simulation, fault-injected: `provider-late-answer` (first audio
  16 s after endpoint, right-censored at the 15 s deadline, 3/3) and
  `provider-supply-gap` (a 400 ms stall becomes an audible playback gap, 3/3).
  `provider-slow-first-audio` (3.5 s) and `slow-response-then-follow-up` (9 s,
  then a normal follow-up) complete.
- **Reproduce:** `ve fake-run --scenario provider-late-answer --scenario provider-supply-gap --out NEW_DIR`
- **Expected:** bounded waiting with an honest notice or retry; a deliberate,
  measured holdback policy for supply stalls (no stock fillers).

## D9. Steady tones score as speech in lamp-live's VAD (Codex; needs physical check)

- **Evidence:** host-only signal probe: a 200 Hz tone reaches ~0.97 speech
  probability and is admitted under the 6 x 0.80 start rule; white noise peaks
  near 0.77 and is not. Host numerics can differ from native ARM64.
- **Reproduce:** `cargo test -p lamp-voice-eval --test fake_runtime signal_mode_uses_lamp_live_vad_on_the_exact_cached_mix`
- **Expected:** notification beeps or music do not open a turn. Needs a physical
  check with real computer audio before acting on it.

## Passing behavior to keep (regression guards)

Quick chat and facts, both follow-up chains (including a follow-up 150 ms after
the answer ends), short yes/no answers, long answers, self-correction, quiet
(-24 dB) and 240 wpm questions (timing mode is level-blind: confirm with cached
audio or physically), noisy and typing questions, topic changes at 1.2 s and 4 s,
three repeated interruptions in a row, finishing an unfinished question after
Lamp started replying, and the fixed-reply echo-only window with perfect echo
cancellation. Voice and ring state stayed consistent through every cancellation
in 123 attempts with ring cues (no stale output or cue, no overlapping replies,
no unterminated turns).

## Needed from other workstreams to test these physically

- **P1 (Codex):** pass `--cue-socket` through `lamp-live directed` and add
  `input_admitted` and `local_endpoint` cue kinds. Until then, Gemini follow-ups,
  short answers, interruptions and acknowledgments can only run with the
  fixture provider, and those scenarios are withheld rather than run on fixed delays.
- **P3 (Agent 1/Codex):** test-only provider delay, stall and disconnect injection
  so D5–D8 can be repeated on hardware.
- Lamp access for the fixture-provider scenarios, recorded both with synthetic
  loudspeaker playback and with a direct-human cohort; Jieli's onboard processing
  may treat the two differently, so they are reported separately.
