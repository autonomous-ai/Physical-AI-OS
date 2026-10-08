# Hand-off: Lamp voice redesign, live test

Written 2026-10-09 for the session that runs the first live test on a lamp.
Everything below is on branch **`lamp-release`** (origin, `471a48fb`, four
commits ahead of `main`); the working tree was clean and all gates green
(Go build/vet/test, HAL lint, HAL 4,192 tests) when it was pushed. Nothing in
this branch has been run on a lamp yet.

## 1. What the owner asked for

A voice interaction that feels like talking to a person: as close to zero
latency as the stack allows, no awkward fillers, reliable in "just talk" mode
(no wake phrase), with the body and the light ring doing the acknowledging.
Decisions already made by the owner: **Google's voice end to end** (Gemini Live
native audio for realtime turns, Gemini TTS for the main agent), **speed first**,
**no spoken "uh-huh"/"ah" fillers**. The owner found tap-to-talk reliable but
bad UX, just-talk the best UX but unreliable, and never tried the wake phrase.

The full design and the reasoning: `docs/voice-interaction.md` (VI mirror in
`docs/vi/`). §4.0 is the end-to-end experience map, §6 the status table, §7 the
device-day checklist, §8 the decisions and the three recommendations still
open (codec, wake-word default, Google Search).

## 2. What changed, by commit

- `f2e9387a` launch hardening (not voice): atomic `config.json`, factory reset
  wipes transcripts/history/pairing, refresh tokens only to Autonomous hosts,
  Claude Code/PicoClaw keep user skills, realtime `look` respects a camera the
  user switched off, login throttle, image builds carry the OTA signing key.
- `6ff85902` voice: bare "yeah/no" after a question is the answer; echo-prefix
  strip bounded to 4 s after playback; commit on a ≥ 4-word STT partial while
  the final drains; delegated task answers never muted by later chit-chat; a
  failed voice run says "Sorry, I couldn't finish that one."; `[turn-timing]`
  logs; `scripts/bench/voice_turns.py`; Google voice as the Lamp default;
  backchannel sounds off; wait-filler at 4 s with real words.
- `56b29e8f` voice: STT socket kept warm while someone is around
  (`HAL_STT_KEEPALIVE=presence`); conversation window with the wake word off
  (`HAL_CONVERSATION_WINDOW_S=8`); the device's name anywhere in a sentence
  counts, with a late `[TURN CONTEXT UPDATE]`; Gemini session keepalive switch
  (`HAL_GEMINI_KEEPALIVE_S`, off); the post-reply "trailing tool" grace ends
  1 s after playback instead of holding the mic 6 s; endpoint 1.0 s / 0.6 s
  after a final; `scripts/bench/voice_replay.py`.
- `471a48fb` voice: a dim ring while the conversation window is open after a
  reply; the gaze watcher now also runs with the wake word off (facing
  evidence, onset listening cue, gaze prewarm); "[gasp] Oh, I can think
  again!" only after a brain outage ≥ 20 s.

Shipping-config diffs to know about: `robots/lamp/rootfs/opt/hal/.env`
(`HAL_STT_KEEPALIVE=presence`, `HAL_BACKCHANNEL_FILLERS=`,
`HAL_REALTIME_FILLER_DELAY_S=4.0`, `HAL_SILENCE_TIMEOUT=1.0`,
`HAL_ENDPOINT_SILENCE_S=0.6`) and `robots/lamp/ROBOT.md`
(`voice.tts_provider: gemini`).

## 3. Deploy to the lamp

1. Check out `lamp-release`; `make os-build` (cross-compiles os-server); deploy
   the os-server binary, `hal/`, `robots/lamp/` and `system/web/dist` the way
   this team normally does (`scripts/provision/software-update` on the device
   or the OTA upload targets; see `docs/bootstrap-ota.md`).
2. Restart HAL so `.env` and `ROBOT.md` take effect, then os-server.
3. **TTS provider:** `ROBOT.md` only seeds a config.json that has no
   `tts_provider` key. On an already-configured lamp, set the TTS provider to
   Gemini in Settings (web UI → voice) or the realtime turns will still be
   native Gemini audio while the main agent keeps the old provider.
4. Confirm in the HAL log at startup: `STT keepalive: pre-connected` (once
   presence sees you), `[gaze] watching at`, and no `Backchannel enabled` line.

Rollback is one of: redeploy `main`, or revert single `.env` lines on the
device and restart HAL (`HAL_STT_KEEPALIVE=false`,
`HAL_REALTIME_FILLER_DELAY_S=0.5`, `HAL_SILENCE_TIMEOUT=1.2`,
`HAL_ENDPOINT_SILENCE_S=0.8`, remove the empty `HAL_BACKCHANNEL_FILLERS=`).

## 4. Test protocol (in order)

Record the room for every run: set `HAL_AEC_DUMP_DIR=/var/lib/hal/aec-dump`
in `.env` before starting; the WAVs feed `voice_replay.py` later.

1. **Baseline table first.** Ten conversational questions, ten follow-ups
   within 8 s of a reply, five "yeah/no" answers to a question the lamp asked,
   five requests that need the main agent (calendar, "research X", a device
   action), five with the name mid-sentence ("what time is it, Lamp?"), and
   five minutes of TV or two people talking near it. Then copy
   `server.log` off and run `python3 scripts/bench/voice_turns.py server.log`.
2. **What "good" looks like:** `speech_end_to_first_speech_ms` p50 ≤ 2000 on
   conversational turns (was ~4000); zero dropped "yeah" after a question;
   the task answer always spoken even if you chatted while waiting; the
   failure phrase instead of silence when something breaks; no "Hmm"/"uh-huh"
   unless a reply took more than 4 s; the ring dim-lit for ~8 s after each
   reply; TV speech mostly answered with silence.
3. **Then one flag at a time**, re-running the same script on the same kind of
   turns (lines in `.env`, HAL restart between runs):
   - `HAL_GEMINI_THINKING_LEVEL` (the single largest latency item; the model
     is `gemini-3.8-live-extended-thinking`, LOW; the plain model
     "delegates or stays silent every turn" per os-server, so watch routing).
   - `HAL_GEMINI_GOOGLE_SEARCH=false` (a prompt block routes fresh facts to the
     main agent; expect faster chit-chat, slower weather).
   - `HAL_ADDRESSED_GATE=strict` (drops hands-free speech with no evidence
     before any model; count false responses per hour in the TV test).
   - `HAL_REALTIME_DELEGATE_PREAMBLE=true` ("Let me check…" before handoffs;
     earlier models stopped after the line without calling the tool, so verify
     the tool call still happens: `[turn] route=delegated`).
   - `HAL_GEMINI_KEEPALIVE_S=20` with `HAL_GEMINI_IDLE_PARK_S=0` (keep the
     session warm by ping instead of parking at 45 s; watch for WS 1008 closes).
   - `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S=2` only if follow-ups right after
     a reply still get lost.
4. **From the os-server log:** the distribution of
   `intent Jev decision … decision_ms`; set `jev_intent.timeout_ms` just above
   its p95 (do not cut blind: a Jev hit answers a device command locally).

## 5. Log lines to grep

HAL `server.log`: `[admission] evidence:` (one per turn: what the device knew),
`[admission] strict gate:` (strict drops), `[admission] short answer`,
`[turn-timing]` (three stages per turn), `[turn] route=` (where the turn went),
`[voice-metrics] ack|answer`, `NON_BLOCKING grace cut`, `STT keepalive:`,
`[realtime] prewarm started on`, `[realtime] keepalive ping sent`,
`[gaze] speech-start`. os-server journal: `voice turn failed — telling the
user`, `speech auto-cancelled -- realtime answered a newer turn`,
`intent Jev decision`.

## 6. Things changed blind that deserve a skeptical eye

- Endpoint timings (1.0 s / 0.6 s) and the 4-word early commit: watch for
  cut-offs mid-sentence and for commits on a partial that STT later revised.
- Native Gemini audio as the default voice: first time as the shipping default
  on Lamp; check volume, tempo and the speaking ring (`native_play_*`).
- Presence-aware STT keepalive: check the socket really closes when the room
  empties (`nobody around — releasing the socket`) and that an idle lamp is
  not holding a connection all night.
- Gaze watcher in just-talk mode: it also triggers the remembered-pose
  reacquire at speech start; make sure the head motion is welcome, not jumpy.
- Grace cut after playback: a trailing `delegate_to_main` arriving more than
  ~1 s after the reply finished would now be lost; look for `[turn]
  route=realtime_handled` turns that should have been tasks.
- Reconnect notice gating: a real outage under 20 s is now silent (by design).

## 7. Still open (needs the owner or hardware)

- **Codec for the standard Lamp**: full duplex (interrupt it by voice, no deaf
  time) needs a shared-clock mic+speaker with hardware AEC; recommended, see
  design §8. Until then tap-to-interrupt is the only interruption.
- **Wake word default**: recommended off with the evidence gate, `strict` once
  the TV test shows < 1 false response per hour.
- **Google Search**: recommended off in the realtime session unless the A/B
  shows < 500 ms cost on most turns.
- **Result injection** for narrated tasks: blocked, the extended-thinking model
  closes the session (1007) on a scheduled tool response; the announce path
  is the fallback once the model is faster.
- **Proxy idle timeout** on `/ws/gemini`: cross-team; the keepalive switch is
  there to test against it.

## 8. Where to look

Design: `docs/voice-interaction.md` · realtime detail: `docs/realtime-voice.md`
· metrics: `docs/voice-metrics.md` · bench: `docs/benchmarks.md` ·
tests for the new pieces: `hal/test/test_addressed_gate.py`,
`test_turn_admission.py`, `test_early_commit.py`, `test_stt_warm.py`,
`test_realtime_keepalive.py`, `test_gemini_nonblocking_grace.py`,
`test_prewarm_and_addressed.py`, `test_voice_replay.py`,
`system/lib/reconnect/notice_test.go`,
`system/server/agent/delivery/http/handler_voice_failure_test.go`.

Report back with: the `voice_turns.py` table before and after each flag, the
false-response count in the TV test, anything the lamp said that it should
not have, and the AEC dump folder.
