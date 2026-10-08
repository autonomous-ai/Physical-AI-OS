# Voice interaction: talking to Lamp like a person

This document is the design for Lamp's voice loop: what "good" means in
numbers, what the shipping loop does today, why it falls short, and the
redesign. The code is the source of truth for what is implemented; the
*Status* column in §6 says which parts exist.

Vietnamese: [`vi/voice-interaction_vi.md`](vi/voice-interaction_vi.md).
Related: [`realtime-voice.md`](realtime-voice.md) (the realtime layer in
detail), [`voice-metrics.md`](voice-metrics.md) (KPIs and the telemetry
tracker), [`benchmarks.md`](benchmarks.md) (how to measure on a robot).

## 1. The bar: what a person-like voice loop does

People answer each other within about 200 ms, and when they need longer they
show it (a glance, a nod, "hm") rather than going still. Lamp has a face, a
light ring and a head, so it can do the same. The bar, as behaviors with
budgets measured from the moment the user stops speaking:

| Behavior | Budget | Why |
|---|---|---|
| **Attention**: the ring and head show it is listening while the user talks | ≤ 200 ms after speech starts | Local signal, no network |
| **Acknowledgement**: the ring and head show the turn was taken | ≤ 300 ms after speech ends | Makes a longer wait feel attended |
| **Answer** (conversation, knowledge, lookup) | first word p50 ≤ 1.2 s, p90 ≤ 2.0 s | A warm model and streaming speech make this possible |
| **Task** (calendar, device action, research): says what it is doing, then the result | "doing it" ≤ 1.5 s; result spoken when ready, even if the user chatted meanwhile | How a person handles an errand |
| **Addressing**: responds when spoken to, quiet for TV and other people | admitted turns answered ≥ 95 %; false responses to background speech rare | Name, gaze, an open conversation or an answer to its own question are evidence |
| **Interruption**: stops when the user speaks over it (tap always; voice when the echo path allows) | audio stops ≤ 200 ms | A person stops mid-sentence |
| **No silent failure**: heard but cannot answer → says so | always | Silence is the worst outcome |
| **Order**: one answer per question, in order; new chit-chat never erases an in-flight task's answer | always | |

The existing KPIs in [`voice-metrics.md`](voice-metrics.md) stay the scoreboard:
KPI-1 (acknowledged within 3 s) ≥ 95 %, KPI-2 (stale replies played) < 5 %,
KPI-3 (voice tasks that finish) ≥ 85 %. On 2026-09 device logs KPI-1 was 65 %.

## 2. What ships today

The README describes the design as *one voice, three ways to act*: a realtime
model (Gemini Live) answers conversation directly; anything that needs skills,
memory or hardware is delegated to the main runtime (Hermes by default); computer
work goes through Harness and the result comes back as a short spoken summary.
The intent is sound: a fast model for the moment, a capable agent for the work.

The shipping standard Lamp (`robots/lamp/rootfs/opt/hal/.env`, `ROBOT.md`)
runs the **turn path**: `HAL_LIVE_MODE=false`, wake word on for a fresh
config, Gemini `gemini-3.8-live-extended-thinking` at thinking level LOW,
Google Search grounding on, native audio off (Gemini's audio is discarded and
its transcript is re-voiced by ElevenLabs over HTTP), STT through the
Autonomous gateway on a fresh WebSocket per turn.

What one utterance goes through, from code reading plus device numbers from
PRs #276, #454, #473, #562, #602, #608 and `realtime-voice.md`:

| Stage | Serial wait | Typical |
|---|---|---|
| Entry VAD → STT socket opens, mic frames buffered | first partial 1.5–2.5 s | hidden behind speech |
| Endpoint: silence 1.2 s, or 0.8 s after an STT final; Smart Turn fallback 2.5 s | **0.8–2.5 s after the last word** | 0.8–2.5 s |
| STT close and final drain (opener turns) | 0.2–1.0 s | |
| Noise guard (Silero over the whole buffer, ≤ 3 words), speaker-ID join | 0.2–0.6 s | |
| Session prepare: parked after 45 s idle → reconnect; quarantined after every delegate → rebuild | 0–4 s | hits ~45 % of turns (8/18, #473) |
| Gemini first sentence | **median 4.0 s, none under 3.0 s** (n=31, lamp-0c89) | 3–6 s |
| ElevenLabs path holds a finished sentence until the next chunk or generation end; clause streaming off | 0–2 s | |
| ElevenLabs HTTP first byte, 1.2× tempo, ALSA | 1.8–2 s | |
| After the reply: 6 s NON_BLOCKING grace plus an outcome check **on the capture thread** | mic not read | 0–16 s |

End to end, measured on lamps: conversation **median 4 s** (3–8 s); delegated
turns **6–22 s with no sound**; a follow-up right after a reply can be missed
because the mic is not being read. Roughly 110 timers, thresholds and flags sit
on the path, and 22 distinct gates can drop an utterance silently (wake final
mismatch, filler-only words, ≤ 3 words with a low voiced ratio, empty
transcript, echo prefix, similarity to the last reply, rebuilding session,
lookback cleared after playback, mic drained during the grace, …).

Five fix → regression → fix chains ran through this path between 2026-09-03
and 2026-10-08 (fillers, live mode, first-clause speaking, delegation prompts,
the wake/focus gate), almost all verified with mocks or a single lamp.

## 3. Why it falls short

1. **Two brains in series, routed by prompt.** The realtime model decides per
   turn, from ~100 lines of prompt per provider, whether to answer, delegate or
   stay silent. Delegation means: wait for the thinking model, emit a tool
   call, forward text to os-server, run intent matching, forward to Hermes, wait
   for its first sentence, synthesize. The model is told to say nothing while
   handing off, and the main-agent fillers are paused, so the user hears
   silence. Every delegate also quarantines the session, so the next capture
   pays a reconnect.
2. **A serial commit path with no overlap.** Cold STT sockets, the final drain,
   the noise guard, the speaker-ID join, the session prepare and the
   announcement drain all run one after another between the last word and the
   commit. The thinking model, the sentence hold and the double synthesis then
   add seconds between the commit and the first sound.
3. **No deterministic answer to "is this for me?".** With the wake word off every
   trigger is "addressed"; with it on, a 5 s follow-up window and the gaze
   opener are the only non-verbal evidence. Whether the lamp answers "yeah" after
   asking a question, or ignores the TV, is left to the model and to a word list.
4. **No shared turn identity between the two brains.** Replies, fillers and
   history are matched by time ("newest interaction", watermarks), which is why
   stale and duplicated answers keep coming back in different forms.
5. **Half-duplex on the standard Lamp.** The mic and speaker are separate USB
   devices with free-running clocks; software AEC manages ~6 dB, echo peaks
   above real interruptions, so voice barge-in was removed and the mic is drained
   while the lamp speaks. The Pro profiles (XVF3800 array with hardware AEC)
   already run live mode.
6. **No measurement loop.** No recorded-audio corpus, no replay harness,
   analytics off on the lamp, KPI-1 not computable in live mode. Policy swings
   on feel.

## 4. The redesign: one conversation, three latencies

The user should experience **one person** who answers instantly, does small
things while talking, and says "give me a second" for big things and comes back.
Behind that, keep the two engines, but change who owns the conversation:

```
                 ┌──────────────── Voice agent (owns the conversation) ───────────────┐
 mic ──► local   │  always-warm session · native speech or streaming TTS · one state   │ ──► speaker
        gate ──► │  machine · admission evidence in, one answer stream out             │     ring/head
                 └──────┬──────────────────────┬──────────────────────┬───────────────┘
                        │ instant              │ fast (≤100 ms)       │ slow (seconds–minutes)
                        ▼                      ▼                      ▼
                  answer itself         device tools (HAL)      run_task → main runtime / Harness
                                        LED · look · move         result comes back INTO the
                                        volume · timers           conversation; the voice agent
                                                                  narrates: "I'll check" … "Two
                                                                  meetings tomorrow: …"
```

The three routes of the README collapse into one loop with tools of three
latency classes. The main runtime stops being a second speaker; it is a worker
whose result the voice agent speaks, in order, in one voice.

### 4.0 The experience, end to end

What the user sees and hears at each moment, the budget, and where it lives.
Rows marked *device* are confirmed on a lamp; the rest are code-level.

| Moment | What Lamp does | Budget | Where |
|---|---|---|---|
| Someone walks in / looks at it | STT socket pre-connects; a parked Gemini session resumes; presence greeting rules as before | before the first word | `stt_warm.py`, `prewarm.py`, `gaze.record_sample` |
| User starts talking | ring dims to "listening" at once when facing the lamp (wake word off) or on the wake phrase; head reacquires the speaker | ≤ 200 ms | `_vad_loop`, `show_listening_pending_cue`, `gaze` |
| User says its name mid-sentence | counts as addressed, even without the wake phrase | — | `turn_admission.name_mentioned` |
| User stops talking | endpoint at 1.0 s silence / 0.6 s after an STT final; commit on a ≥ 4-word partial; thinking face + ring | ≤ 300 ms to the cue | `_stream_session_impl`, `run_realtime_turn` |
| Quick answer (time, volume, lights) | local rule, cached phrase, no model | < 1 s | `system/intent` |
| Conversation / knowledge / lookup | Gemini Live answers in its own voice (native audio); long replies stream | first word p50 ≤ 1.2 s target; 4 s today (*device*) | `realtime_turn.py`, `gemini_live.py` |
| Bare "yeah" / "no" after Lamp asks | treated as the answer | — | `turn_admission.device_question_pending` |
| Slow reply | ring + face hold "thinking"; one spoken bridge ("One sec.") only after 4 s | — | `_WaitFiller`, `fillers.go` |
| Task (calendar, device, research) | handed to the main agent; optional "Let me check…" line; result spoken in the same Google voice when ready, never muted by later chit-chat; failure spoken | ack ≤ 1.5 s target; result 6–22 s today (*device*) | `delegate_to_main`, `IsTaskRun`, `speakVoiceTurnFailure` |
| Follow-up right after a reply | mic is read again ≤ 1 s after playback ends; the conversation window (8 s) keeps the next sentence addressed without the name | — | grace cut, `CONVERSATION_WINDOW_S` |
| TV / other people | no name, not facing, no window, no pending question → model is told "unknown — stay silent"; `strict` drops it before any model | — | `addressed_hint`, `HAL_ADDRESSED_GATE` |
| Interruption | tap stops speech ≤ 250 ms (*device*); voice barge-in needs hardware AEC | — | `button_actions`, §4.5 |
| Lamp cannot answer | says so ("Sorry, I couldn't finish that one.") instead of silence | — | `agent.voice_turn_failed` |
| Idle room | STT socket released, Gemini session parked (or kept alive by ping when enabled) | — | `stt_warm`, `_maybe_keepalive` |

### 4.1 Turn controller

One state machine per device, `IDLE → LISTENING → ENDPOINTING → THINKING →
SPEAKING → (follow-up window) → IDLE`, with one **admission decision** per
utterance, logged with its evidence. All of today's gates become inputs to that
decision instead of independent drops:

| Evidence | Source | Effect |
|---|---|---|
| Wake phrase (partial or final) | STT | admit |
| Open conversation window (follow-up after a reply) | controller | admit |
| The device or the main agent just asked a question | TTS history (`turn_admission.device_question_pending`, `main_followup`) | admit, including bare "yes"/"no" |
| User facing the lamp while speaking | gaze (`HAL_GAZE_WAKE`) | admit |
| Enrolled voice of a known user | speaker ID | raise confidence |
| Voiced duration and ratio | Silero | reject short noise bursts |
| Words overlap the device's own last reply within the echo window | TTS history | strip the prefix, or reject a pure echo |
| None of the above, wake word on | — | reject (not addressed) |
| None of the above, wake word off | — | admit; the model is told `addressed: unlikely` and may stay silent |

The model still gets ambiguous cases, but with a hint and a smaller job: it no
longer has to infer from scratch, each turn, whether the room is talking to it.

### 4.2 Always warm

A person who is in the room is ready to be spoken to. The session is
pre-connected whenever presence or gaze says someone is here, not only when
speech starts; recycles and context compaction run in the background between
turns, never on the commit path; a keepalive stops the proxy from closing an
idle socket, so "parked after 45 s" disappears for an occupied room. This needs
one change outside this repo: the `/ws/gemini` proxy idle timeout raised to the
Live API's own session limit.

### 4.3 The commit path

From the last word to the first sound:

1. **Endpoint** on the first of: Smart Turn says complete, 0.6 s of silence after
   an STT final, or a bounded silence fallback. Hesitations ("umm…") hold.
2. **Acknowledge locally** the moment the turn is admitted: thinking ring and a
   small head motion, before any network wait.
3. **Commit immediately.** A confident STT partial (≥ 4 words) admits the turn;
   the STT final drains in parallel and only feeds history and the fallback.
   Speaker ID and the noise guard never block the commit of a long utterance.
4. **Stream speech.** First clause as soon as it exists; no holding a finished
   sentence for a possible late tag; native audio (Gemini's own voice) or
   ElevenLabs over WebSocket (`eleven_v4_turbo`, ~300 ms first byte) instead of
   HTTP; the output stream stays open between sentences.
5. **No post-reply blackout.** The grace for a late tool call runs on a
   background task; the mic is read again as soon as playback ends.
6. **Thinking off for conversation.** The extended-thinking model is used
   because the plain model over-delegates or stays silent; with the
   deterministic gate doing admission and routing hints, thinking can be turned
   down or off for direct answers. Measured on device, not assumed.

### 4.4 Tasks: narrated, in order, never lost

`delegate_to_main` becomes a **non-blocking tool**: the voice agent says what
it is doing in its own words ("I'll check your calendar"), keeps listening, and
when the main runtime's result arrives it is delivered as the tool's response,
so the voice agent speaks it, in the same voice, at the right moment. The
main runtime never speaks on its own in voice mode; it returns text. Order is
the voice agent's own conversation order, so there are no watermarks and no
"[voice-instruction]" transcripts. Failures come back the same way ("I couldn't
reach your calendar"), never as silence. Gemini Live supports this as
`NON_BLOCKING` function calls with a scheduled response; the extended-thinking
model already requires NON_BLOCKING tools.

Until that lands, two rules hold the line in the current code: a delegated
task's answer is never muted by later chit-chat (`IsTaskRun`), and a failed
spoken request is announced (`agent.voice_turn_failed`).

### 4.5 Full duplex and interruption

Live mode (continuous uplink, provider VAD, ~100 ms to first audio) is the
target for every body with hardware echo cancellation; the Pro XVF3800 profile
runs it today. The standard Lamp's separate USB mic and speaker cannot reach
that with software AEC. Two options, in order of preference:

1. **Hardware**: a combined USB audio codec with on-chip AEC (mic and speaker on
   one clock), the same class of part conference speakerphones use. This is a
   bill-of-materials decision for the standard SKU.
2. **Software**: keep the turn path but open the mic during playback behind the
   adaptive gate (`live_gate.py`): duck the speaker when a voiced onset is
   confirmed, cancel the reply only when the provider's VAD confirms speech.
   Backchannels ("uh-huh") duck but do not cancel.

Tap-to-interrupt stays as the always-works path on every body.

### 4.6 Acknowledgement and presence

Non-verbal first: the ring and head carry "listening", "got it", "thinking",
"speaking" and "couldn't" states; a spoken "Hmm" only when the model is slow
(`HAL_REALTIME_FILLER_DELAY_S`, now 1.5 s on Lamp), never on follow-ups, and
never on top of a real answer. Backchannel sounds only on admitted turns. The
lamp turns toward the speaker when it starts listening (gaze reacquire already
does this) and holds a "thinking" face until the first word.

### 4.7 Measurement

Every utterance writes one timeline: `[voice-metrics] speech end`,
`[turn-timing] speech_end_to_commit_ms / commit_to_first_output_ms /
speech_end_to_first_speech_ms`, `[voice-metrics] ack/answer`, `[turn] route=`,
`[admission]`. `scripts/bench/voice_turns.py server.log` turns a day's log into
a per-turn table with p50/p95 per stage and the drop reasons. Device sessions
record room audio (`HAL_AEC_DUMP_DIR`, `HAL_LIVE_UPLINK_DUMP_DIR`) so endpoint
and admission changes are replayed against the same clips before they ship:
`scripts/bench/voice_replay.py room.wav` runs a recording through the entry
gate, the silence clock and the noise guard and prints every utterance the
lamp would have opened and when it would have decided the user stopped.
Analytics on (`AUTONOMOUS_ANALYTICS_URL`) so KPI-1/2/3 come from the fleet.

## 5. Lessons from the best voice products

From a product and literature review on 2026-10-08 (ChatGPT Voice / OpenAI
Realtime and GPT-Live, Sesame, Kyutai Moshi and Unmute, Hume EVI, Gemini
Live, Alexa+ and other smart speakers, the Pipecat / LiveKit / Vapi /
ElevenLabs stacks, and turn-taking research). Numbers are as the sources
reported them; "claimed" means a vendor figure, "measured" an independent one.

1. **The budget is 200 ms gaps, 800 ms voice-to-voice, 4 s before fillers help.**
   Human turn gaps are modal 0–200 ms (Stivers 2009, Levinson & Torreira 2015);
   gaps read as reluctant from ~600–700 ms and ~1 s is the "standard maximum".
   The stacks converge on an 800 ms median voice-to-voice target (network 200 /
   endpoint+STT 400 / LLM 500 / TTS 200 ms, Daily 2025); the only large
   production sample (Hamming, Jan 2026) shows p50 1.4–1.7 s, p95 4.3–5.4 s.
   A CUI'25 study (n=54) found spoken fillers only help perceived
   responsiveness at delays ≥ 4 s and never improve perceived competence; eager
   filler systems overlapped users 47.9 % of the time. → §1 budgets, §4.6.
2. **Nobody has solved turn-taking; everyone layers it.** Sesame's TurnBench
   (Aug 2026, 14 systems): "no system is fast, selective and high-recall at the
   same time"; OpenAI's server VAD had end-of-turn recall 0.955 at a 0.525
   false-positive rate. Every stack uses a short silence (200–500 ms) plus a
   learned end-of-turn model plus a long backstop (1.3–6 s); Smart Turn v3 is
   8 MB and 12–60 ms on CPU; Deepgram Flux "eager" end-of-turn fires 150–250 ms
   earlier at +50–70 % model calls. Alexa abandoned fixed timers in 2018 for
   acoustic + partial-transcript + decoder features and a second-pass
   arbitrator (2024) that cut early endpoints 16 %. → §4.3 step 1.
3. **Say one short line, then call the tool; delegate in the background.**
   OpenAI's realtime prompting guide: "Before any tool call, say one short
   line … then call the tool immediately." GPT-Live (July 2026) decides many
   times per second whether to speak, listen or call a tool, and delegates
   reasoning to a backend model while still talking; the leaked Alexa+ design
   target was 2–4 s with reviewers measuring up to 15–23 s of silence on
   agentic tasks, which is exactly the failure to avoid. → §4.4.
4. **Precision for speech, recall for attention.** Looking at a robot is not
   addressing it: only 65 % of robot-facing utterances were for the robot
   (Katzenmaier 2004), while head pose + speech reached 92 %. Amazon's
   Conversation Mode (audio VAD + head orientation, 2021) cut ambient false
   wakes 80 % and self-speech false wakes 42 % with no added latency; Apple's
   dialogue-context features cut false accepts 20–40 % at 10 % false
   rejects. TV audio misactivates smart speakers ~0.95 times per hour (PETS
   2020). Fixed-schedule "mm-hm" backchannels on Alexa raised "it listens"
   (3.91 vs 2.56) but also irritation (3.70 vs 2.25). The policy that follows:
   non-verbal attending can be generous, spoken replies must be sure. → §4.1, §4.6.
5. **Echo cancellation is the client's problem, and clocks are the problem.**
   OpenAI documents no server-side echo handling; speaker-plus-mic devices
   loop on their own voice without client AEC, and the only field workaround
   was muting the mic during playback. HomePod's echo sits 30–40 dB above
   far-field speech and needs a DNN residual suppressor on top of linear AEC.
   Consumer crystals drift ±50–100 ppm (~96 samples per minute at 16 kHz); a
   100 ms adaptive filter absorbs ~20 ppm, so a USB mic and a separate USB
   speaker defeat software AEC by construction. The XVF3800 solves it by
   putting the speaker output on the array's own clock. → §4.5.
6. **Interruption policy, not interruption detection.** LiveKit: minimum 0.5 s
   of speech, adaptive barge-in, a 2 s false-interruption timeout that resumes
   the reply, and history truncated to what was actually heard. Vapi ships
   explicit "never interrupt" (acknowledgement words) and "always interrupt"
   phrase lists. GPT-4o Realtime stopped within 0.23 s for interruptions but
   also for 91–93 % of side speech; Nova Sonic and Gemini ignored 93–99 % of
   backchannels but took > 2 s to yield. Duck on onset, cancel on confirmed
   speech. → §4.5.
7. **Stream everything; hold nothing.** Sentence aggregation costs ~200–300 ms
   per sentence (Pipecat); ElevenLabs Flash over WebSocket is ~75 ms inference
   and 100–200 ms regional first byte with a chunk schedule of [120, 160, 250,
   290] characters; Gemini 3.8-live is documented as native-audio only, so
   discarding its audio to re-synthesize text is pure added latency. Community
   numbers for Gemini Live: ~100 ms first token and 1–1.5 s turnaround with
   tools off, 2–3.5 s with Google Search grounding on. → §4.3 steps 4 and 6.
8. **Light and motion carry the state, earcons are optional.** Every shipping
   speaker has a distinct light for listening / thinking / speaking / open mic
   / error (Echo: voice-modulated blue pulses, a travelling "thinking" light;
   Nest: a "wake word not understood" flash); fire the listening cue from the
   local trigger (≤ 100 ms), keep the open-mic window 5–8 s with its own hue,
   close it early on "thanks" / "stop". Amazon's guidance: implicit
   confirmation by default, explicit only for costly actions, "one-breath"
   replies; Google's: never repeat verbatim, end after two failed attempts,
   never say "I didn't hear you". → §4.6.
9. **Keep a deterministic fast path.** Stop, volume, timers and lights should
   never wait for a model (Alexa's 800 ms p50 / 1,000 ms p90 ceiling for
   smart-home actions). The local intent table already exists; it should
   also own the interrupt. → §4 "fast" tools.
10. **What users call alive vs robotic.** Alive: speed, initiative with
    restraint, memory, context-appropriate imperfection, a stable persona.
    Robotic: over-talking and filling silences, off cadence, catchphrases and
    sycophancy, no dynamic range, hearing its own echo. Sesame's own
    post-mortem (2025): timing "wrong more often than right" and inconsistent
    personality were what broke the illusion. → §1, §4.6.

## 6. Status and rollout

| Item | Status (2026-10-08) |
|---|---|
| Bare "yes/no/okay" admitted when the device or main agent just asked (`HAL_DEVICE_QUESTION_WINDOW_S`, 12 s) | **implemented** |
| Echo-prefix strip limited to captures that start within `HAL_ECHO_PREFIX_WINDOW_S` (4 s) of playback end | **implemented** |
| Commit on a confident STT partial (`HAL_EARLY_COMMIT_MIN_WORDS`, 4) while the final drains | **implemented** |
| Thinking cue fired before the commit path's waits, not after them | **implemented** |
| `[turn-timing]` lines and `scripts/bench/voice_turns.py` | **implemented** |
| Offline replay of recorded audio through the entry gate, silence clock and noise guard | **implemented** (`scripts/bench/voice_replay.py`) |
| STT socket kept warm while someone is around (`HAL_STT_KEEPALIVE=presence`): first partial no longer pays a cold connect, short utterances survive | **implemented** (`stt_warm.py`, Lamp `.env`) |
| Gemini session keepalive ping as an experiment switch (`HAL_GEMINI_KEEPALIVE_S`, default off) | **implemented** |
| Conversation window when the wake word is off (`HAL_CONVERSATION_WINDOW_S`, 8 s): the next sentence after a reply is addressed without the name | **implemented** |
| The device's name anywhere in the sentence counts as addressed; a late name sends a `[TURN CONTEXT UPDATE]` | **implemented** |
| Listening cue at speech onset when the user is facing the lamp (wake word off) | **implemented** |
| A dim ring stays on while the conversation window is open after a reply ("still with you") | **implemented** (`_show_conversation_window_cue`) |
| "Oh, I can think again!" only after a brain outage of ≥ 20 s; planned restarts stay silent | **implemented** (`system/lib/reconnect`, all six runtimes) |
| A delegated task's answer survives realtime answering a newer utterance | **implemented** (os-server `IsTaskRun`) |
| A failed spoken request is announced instead of silence | **implemented** (`agent.voice_turn_failed`, 20 s debounce) |
| Non-verbal acknowledgement on Lamp: no spoken "uh-huh" while the user talks (`HAL_BACKCHANNEL_FILLERS=`), one spoken bridge only after 4 s (`HAL_REALTIME_FILLER_DELAY_S=4.0`), bridge phrases are words ("One sec.", "Still thinking.") not noises | **implemented** (`.env`, `fillers.go`) |
| Google's voice end to end on Lamp: Gemini Live native audio for realtime turns, Gemini TTS (same voice) for the main agent's text | **implemented** (`ROBOT.md` `tts_provider: gemini`) |
| Endpoint 1.0 s of silence or 0.6 s after an STT final on Lamp | **implemented** in `.env`; re-measure on device |
| Presence- and gaze-driven prewarm of a parked session | **implemented** (`prewarm_realtime` from `presence.enter` and facing gaze samples) |
| Admission gate v2: evidence (name, conversation window, pending question, facing, known voice) gathered once per turn, logged, sent to the model as an `Addressed:` line; `strict` mode drops hands-free speech with no evidence before any model | **implemented** (`HAL_ADDRESSED_GATE=hint` default, `strict` for device trials) |
| Safe "search off": a prompt block that routes fresh-fact questions to the main agent when `HAL_GEMINI_GOOGLE_SEARCH=false` | **implemented** (`NO_SEARCH_PROMPT`) |
| Opt-in delegate preamble: the model says one short line about what it is about to do, then calls `delegate_to_main` in the same turn | **implemented** as a device-day switch (`HAL_REALTIME_DELEGATE_PREAMBLE`, default off) |
| Thinking level / plain model for direct answers | device-day experiment (`HAL_GEMINI_THINKING_LEVEL`, `HAL_GEMINI_LIVE_MODEL`) |
| Google Search on vs off (latency vs direct lookups) | device-day A/B (`HAL_GEMINI_GOOGLE_SEARCH`) |
| Post-reply deaf time: the trailing-tool grace ends once the reply has been played and the speaker idle for 1 s, instead of running its full 6 s | **implemented** (`HAL_REALTIME_GRACE_AFTER_PLAYBACK_S`) |
| Proxy idle timeout for an occupied room | planned (cross-team); the client ping is in place to test against it |
| Non-blocking delegation with result injection | blocked on the model: the extended-thinking model closes the session (1007) on a scheduled function response; the fallback is the announcement path, measured once native audio is on |
| Live mode by default on hardware-AEC bodies; codec decision for the standard Lamp | product decision (§8) |

## 7. Device day (next session with a lamp)

1. Pull `server.log` and `local/flow_events_*.jsonl`; run
   `python3 scripts/bench/voice_turns.py server.log` for the baseline table.
2. Ten conversational turns, ten follow-ups, ten delegated tasks, five "yeah"
   answers, five TV/background clips, with `HAL_AEC_DUMP_DIR` set. Keep the WAVs.
3. Flip one flag at a time, re-run the same script, compare p50/p95:
   `HAL_GEMINI_THINKING_LEVEL`, `HAL_TTS_ELEVENLABS_WS=true`,
   `HAL_REALTIME_NATIVE_AUDIO=true`, `HAL_ENDPOINT_SILENCE_S=0.6`,
   `HAL_TURN_END_FALLBACK_S=1.5`, `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S=2`.
4. From the os-server log, the distribution of `intent Jev decision … decision_ms`:
   that synchronous classification (budget `jev_intent.timeout_ms`, 3 s cap) sits
   in front of every voice turn no local rule matched. Set the budget just above
   its p95; it must not be cut blind, since a hit answers a device command locally
   instead of through a 6–22 s main-agent turn.
5. Acceptance for the day: answer p50 ≤ 2 s on conversational turns, zero
   dropped "yeah" after a question, zero muted task answers, every failure spoken.

## 8. Decisions

Decided (2026-10-08):

- **Voice identity: Google.** Gemini Live's native audio for realtime turns and
  Gemini TTS, same voice, for the main agent's text. No second synthesis on the
  conversational path; the landing page's "choice of voices" line changes.
- **Speed first.** Where a choice trades latency for anything else, the latency
  cost is stated and speed wins unless correctness is at stake.
- **No spoken fillers.** Listening and thinking are shown by the ring and head;
  a spoken bridge is real words and only after 4 s of waiting.

Recommended, with the reasoning, for the owner to confirm:

- **Standard Lamp audio: change the codec.** "Like talking to a person" needs
  the lamp to hear while it speaks: interruption, no deaf time after a reply, no
  echo-skip. With a USB mic and a separate USB speaker that is not a tuning
  problem but a clock problem (±50–100 ppm drift defeats the adaptive filter;
  measured ~6 dB of cancellation), so software AEC will stay a half-measure.
  A shared-clock part with on-chip AEC (XVF3800 class, speaker driven from the
  array board) is what the Pro profile already runs live mode on. Until the
  standard SKU changes, ship half-duplex with the gated mic and tap-to-interrupt,
  and say so honestly. If the BOM cannot change before launch, plan it for the
  next run; it is the single hardware change with the biggest UX effect.
- **Wake word: off by default, with the evidence gate.** A person does not need
  a password; they respond when you look at them, say their name, are mid
  conversation with you, or just asked you something. That is exactly the
  evidence the gate collects. The cost is false responses to TV and other
  people: without any gate, keyword-free systems false-accept hundreds of
  utterances an hour; with multimodal gating Amazon cut ambient false wakes by
  80 %. So: default off, `HAL_ADDRESSED_GATE=strict` on the standard Lamp once
  a day of device logs shows false responses below one per hour in a TV room
  (`hint` mode, the safer default, until then), and the wake phrase always
  works as an override. Keep "wake word on" as the setting for shared rooms.
- **Google Search: off in the realtime session for now.** Community numbers put
  Gemini Live turnaround at 1–1.5 s with tools off versus 2–3.5 s with Search
  on, and grounded answers were spoken before the search returned on this
  device (#277). With the no-search prompt block, fresh-fact questions go to
  the main agent, which is slower but correct. Confirm with the A/B in §7; if
  the measured cost of Search on is under 500 ms on most turns, turn it back on.

## 9. Research sources

The lessons in §5 come from a sourced review (2026-10-08) kept with the
engineering notes: OpenAI developer docs and forum (Realtime API, GPT-Live,
prompting guide), Sesame's "Crossing the uncanny valley of conversational
voice" and TurnBench, Kyutai's Moshi paper and Unmute posts, Hume EVI docs,
Google's Live API docs, Amazon Science (end-pointing, device-directed speech,
Conversation Mode), Apple ML research, the Pipecat / LiveKit / Vapi /
ElevenLabs / Deepgram / Krisp docs and benchmarks, Full-Duplex-Bench, and
turn-taking literature (Stivers 2009; Levinson & Torreira 2015; Katzenmaier
2004; CUI'25 filler study). Vendor claims and independent measurements are
marked as such in §5; the report itself is not part of this repo.
