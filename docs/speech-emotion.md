# Speech Emotion Recognition (SER)

Recognize the **user's** emotion from their voice (not the device's). At the end of every mic session (VAD trigger → ~2.5 s silence stop), `SpeakerDecorator.submit_speech_emotion_from_session` builds a mono 16 kHz WAV from the session's untrimmed audio snapshot and enqueues it on `SpeechEmotionService`, which prefilters the audio locally, classifies it in the cloud, buffers per user, dedups by polarity bucket, and fires `speech_emotion.detected` sensing events to the OS server. **SER runs independently of STT** — laughter, sighs, "uh-huh"s, and other non-verbal cues that leave the transcript empty still classify. The `user` field comes from the turn's single speaker recognize (falls back to `unknown` when speaker ID is unavailable, the buffer is too short for an embedding, or there's no match); it never gates whether SER runs.

This is the voice-side twin of facial emotion detection (`emotion.detected`). The architecture, polarity bucketing, and dedup window are intentionally symmetric so both modalities land in the same downstream skills (`user-emotion-detection/SKILL.md`, mood logging, music suggestion).

> Not to be confused with **Emotion Expression** (`emotion/SKILL.md`) — that controls the device's own emotional output (servo + LED + eyes). SER is about sensing what the *user* feels through speech; expression is how the *agent* shows its feelings.

**Vietnamese:** [docs/vi/speech-emotion_vi.md](vi/speech-emotion_vi.md)

> **This page is the platform-level SER reference** — architecture, guards, buckets, config, debug audio, failure modes. The speech-service doc set goes deeper on two axes:
>
> - **[docs/speech/speech-emotion-pipeline.md](speech/speech-emotion-pipeline.md)** — every stage with its file:line, the complete prefilter threshold tables, an 18-row drop-reason → log-line index, and a debug playbook.
> - **[docs/speech/speech-emotion-known-issues.md](speech/speech-emotion-known-issues.md)** — open defects and performance problems.
> - [docs/speech/speech-emotion.md](speech/speech-emotion.md) — how SER plugs into a voice turn.
>
> Where docs and code disagree, **the code wins**.

---

## Architecture

```
voice_service._stream_session(...) finally:                      ← every mic session end
    ├─ finalize_session(...) → (combined, ser_audio_buffer, buf_duration)
    │      ser_audio_buffer = UNTRIMMED snapshot (SER keeps laughter / sighs);
    │      audio_buffer is trimmed in place for speaker recognition only
    │
    ├─ speaker-ID prepass: identify_and_decorate(final_text, audio_buffer)
    │      → (final_msg, se_user, display)      ← the ONE recognize this turn
    │
    └─ dispatch_turn(...)                        (_internal/turn_dispatch.py)
           ├─ if combined: sensing_sender.send(final_msg, event_type)
           │      `voice` / `voice_command` / `voice_followup` / `voice_agent_handled`
           └─ submit_speech_emotion_from_session(ser_audio_buffer, user)   ← SER pipeline
                  └─ _session_wav_for_ser(buffer) → (wav, duration_s)
                  └─ SpeechEmotionService.submit(user, wav, duration_s)
                  ▼
SpeechEmotionService.submit(user, wav_bytes, duration_s)   ← non-blocking
    │  4 guards (available / non-empty user / non-empty wav / duration ≥ 3.0 s)
    │  queue.put_nowait                          ← maxsize 32
    ▼
worker thread (daemon)
    │  Emotion2VecRecognizer.recognize(wav_bytes)
    │     ├─ prefilter — RMS trim + voiced gate, then Silero VAD, then ≤8 s most-voiced span   ← LOCAL, drops non-speech
    │     ├─ POST {DL_BACKEND_URL}/hal/api/dl/ser/recognize
    │     │     ← { "label": "happy", "confidence": 0.78 }
    │     ├─ per-label confidence gate
    │     └─ _persist_wav() → debug clip on disk
    ▼
buffer[user].append(_Inference)              ← per-user accumulation
    ▲
    │  (flush thread wakes every SPEECH_EMOTION_FLUSH_S)
    ▼
flush:
    ① drop neutral / <unk> / other labels
    ② mode(label) across this user's buffered samples
    ③ bucket = polarity(mode)                ← positive | negative
    ④ TTL dedup: key=(user, bucket) over SPEECH_EMOTION_DEDUP_WINDOW_S
    ⑤ POST OS server /api/sensing/event with type="speech_emotion.detected"
```

HAL's voice pipeline **only calls `submit()`**. The prefilter, all HTTP I/O to perception-service, buffering, bucketing, dedup, retry, and the OS server POST are contained inside the `speech_emotion/` module — they never block the STT path.

There is a **second** call site: when the wake-word gate rejects a turn, `voice_service.py:1555` submits directly with the default `user="unknown"`. It is only reachable when `WAKEWORD_ENABLED` is true, which defaults to **false** (`hal/config.py:625`) — so in the shipped always-listening configuration every finished mic session reaches SER through `dispatch_turn`.

---

## File Layout

```
hal/drivers/voice/speech_emotion/
├── __init__.py        # public API: SpeechEmotionService + ABC + engine + result type
├── constants.py       # defaults, label vocabulary, bucket map, prefilter thresholds, event type
├── base.py            # BaseSpeechEmotionRecognizer (ABC), SpeechEmotionResult dataclass
├── emotion2vec.py     # Emotion2VecRecognizer — prefilter + HTTP wrapper for /hal/api/dl/ser/recognize
├── utils.py           # normalize_label, is_neutral, bucket_for, hedge_for, format_message,
│                      # wav_to_pcm16, pcm16_to_wav, compute_frame_rms, compute_trim_and_voiced
├── debug_tracer.py    # SER-DEBUG tracer + stage profiler — dev-only, OFF by default, delete before deploy
└── service.py         # SpeechEmotionService — queue + worker + flush + dedup + send to OS server
```

Callers and neighbours:

| Concern | Path |
|---------|------|
| Submit site + service construction | `hal/drivers/voice/_internal/speaker_decorate.py` (`:117`, `:322`, `:343`) |
| Turn dispatch (supplies `user`) | `hal/drivers/voice/_internal/turn_dispatch.py:157` |
| Untrimmed SER snapshot | `hal/drivers/voice/_internal/session_finalize.py:28` |
| Boot-scoped dedup sidecar | `hal/dedup_sidecar.py` |
| Shared Silero model | `hal/drivers/voice/resources/silero_vad.onnx` |
| Cloud model + serving | `integrations/perception-service/src/core/perception/audio_emotion/` |
| OS-server consumer | `system/server/sensing/delivery/http/handler.go` |

Adding a new engine: subclass `BaseSpeechEmotionRecognizer` (one method: `recognize(wav_bytes) -> SpeechEmotionResult | None`) and swap it in via `SpeechEmotionService(recognizer=...)` at construction time. The default factory builds `Emotion2VecRecognizer` from `config.SPEECH_EMOTION_API_URL`. **Note the prefilter lives inside the engine, not the service** — a replacement engine inherits none of it.

---

## Threading Model

Two daemon threads, started in `SpeechEmotionService.__init__` only when `recognizer.available` is true:

| Thread | Loop | Drains | Produces |
|--------|------|--------|----------|
| `speech-emotion-worker` | `_worker_loop` | submission queue (`queue.Queue`, maxsize 32) | per-user buffer entries |
| `speech-emotion-flush` | `_flush_loop` (wait + tick every `SPEECH_EMOTION_FLUSH_S`) | per-user buffer | `speech_emotion.detected` POSTs to the OS server |

Both threads exit cleanly on `stop()` — the worker is poisoned with a `None` sentinel, the flush thread observes the stop event during its `Event.wait` (and therefore skips a final flush, deliberately). All mutable state (`_buffer`, `_last_sent_by_key`, `_last_flush_ts`) is guarded by one `threading.RLock`.

`submit()` is non-blocking by design. If the worker queue is full (32-job backlog) the **new** submission is dropped with a warning — this signals real overload (perception-service wedged or down). Audio is single-utterance, not streaming, so a one-utterance drop is acceptable.

> `available` is `recognizer is not None and recognizer.available`, and for the HTTP engine that is just `bool(url)` — a **configuration** check, not a reachability check. The service can report `available` while the backend is down; every call then fails at the HTTP leg and returns `None`.

Module-level config is read **at import time** (`service.py:85-99`), so tests that patch `hal.config` must do it before importing the module.

---

## Local Prefilter (the only edge model on this path)

Before any network I/O, `Emotion2VecRecognizer.prefilter()` (`emotion2vec.py:215`) gates the clip. Its purpose is to reject **long-but-sparse** audio — a 20-second clip containing two seconds of TV chatter — which emotion2vec would otherwise label confidently and wrongly. It returns the **trimmed, re-encoded** WAV, so the cloud sees the cleaner buffer too.

Decode requires 16-bit PCM at exactly 16 kHz; multi-channel is averaged to mono.

**Stage 1 — RMS** (single pass, `utils.compute_trim_and_voiced`). One 20 ms RMS envelope serves two jobs with two thresholds by design: `PREFILTER_TRIM_RMS = 3500` (strict) anchors the head/tail trim boundary, `PREFILTER_VOICED_RMS = 2500` (lenient) counts voiced frames inside it so whisper/breathy speech still registers. 100 ms of padding is kept around the cut. Drops when the trimmed clip is `< 2.0 s`, total voiced is `< 1.0 s`, or the voiced ratio is `< 0.30` (denominator is the padded-trim span, so a long silent prefix cannot deflate it).

**Stage 2 — Silero VAD** on the trimmed buffer (`emotion2vec.py:417`). Silero v5 contract: 512-sample chunks at 16 kHz with a 64-sample context prepended; LSTM `state` and `context` are rebuilt from zeros every call, so independent invocations never bleed into each other. Drops when Silero-voiced duration is `< 1.0 s`. When Silero is unavailable (missing model, broken ORT) the RMS bar **tightens** from 1.0 s to 3.0 s rather than passing everything through.

**Upload clip** — after both gates pass, `utils.select_voiced_span` keeps only the contiguous **8 s** (`SER_MAX_CLIP_S`) span with the most voiced 20 ms frames (`PREFILTER_VOICED_RMS`). Ties go to the latest span. Clips of 8 s or less are unchanged. The span is a plain slice, never stitched from voiced pieces. perception-service also bounds SER input to 2–8 s, so anything longer would be cropped server-side anyway (#492).

Re-encode failure is fail-open: the original WAV is sent.

All thresholds are compile-time constants in `constants.py:87-135` — **not** env-overridable. Full tables in [docs/speech/speech-emotion-pipeline.md](speech/speech-emotion-pipeline.md#stage-5-6--the-prefilter-the-only-local-model).

> This is the **fourth** Silero session in the HAL process (`voice_service._silero_vad`, `_rt_noise_vad` and `_silence_vad` all load the same file). See [known-issues #5](speech/speech-emotion-known-issues.md#5--a-fourth-redundant-silero-onnx-session).

---

## Wire Format

### Submission (in-process)

```python
service.submit(
    user="alice",                 # enrolled speaker label, or "unknown" (UNKNOWN_USER_LABEL)
    wav_bytes=b"RIFF....WAVE...", # mono 16 kHz WAV from the untrimmed session snapshot
    duration_s=4.2,               # length of audio for the MIN_AUDIO_S gate
)
```

### Engine call → perception-service

```http
POST {DL_BACKEND_URL}/hal/api/dl/ser/recognize
Headers:
  X-API-Key: <key>
  Content-Type: application/json
Body:
  {"audio_b64": "<base64 WAV (mono 16 kHz, prefiltered + trimmed)>", "return_scores": false}
```

Response:

```json
{ "label": "happy", "confidence": 0.9981, "scores": null }
```

Labels (emotion2vec_plus_large, from `/api/dl/ser/labels`):

```
angry, disgusted, fearful, happy, neutral, other, sad, surprised, <unk>
```

Timeout is a hardcoded 15 s (`DEFAULT_API_TIMEOUT_S`) and there is **no retry on this leg** — any transport error, non-200, non-JSON body, or missing `label` returns `None` and the sample is skipped. When `DL_ENCRYPTION_ENABLED` (default **true**) and a public key resolves, the request and response bodies are wrapped by `CryptoSession`; `DL_ENCRYPTION_REQUIRED` (default false) turns a missing key into a hard error at construction instead of a silent plaintext fallback.

### Sensing event → OS server

```http
POST http://127.0.0.1:5000/api/sensing/event
Content-Type: application/json
```

```json
{
  "type": "speech_emotion.detected",
  "message": "Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)",
  "current_user": "alice",
  "audio": "/tmp/hal-speech-emotion/1715587812413_alice_sad.wav"
}
```

The raw `Speech emotion detected: <Label>.` prefix is the parser anchor for OS-server-side routing. The parenthetical is a hedge clause to stop the LLM from over-committing on noisy SER reads — same pattern as the facial `Emotion detected: …` message. Hedge text is chosen by bucket: `do not over-celebrate` (positive), `do not assume the user is distressed` (negative), `do not over-react` (other).

The `audio` field is a **separate, optional** field — the on-disk path of the WAV clip that produced this event. It is **not** embedded in `message` and is **never** forwarded to the LLM (see [Debug Audio Persistence](#debug-audio-persistence) below). Empty when persistence is disabled or the write failed.

Retry policy: 5 s timeout, 3 attempts with 2 s back-off on `ConnectionError` or HTTP `503`. Other 4xx/5xx are logged and dropped (the sample is gone — we don't retry-storm the OS server). The back-off sleeps on the flush thread, so an unreachable OS server delays the next flush tick by up to ~4 s.

---

## Debug Audio Persistence

To make noisy SER reads debuggable, the service persists the WAV clip behind each event and surfaces it in the Flow Monitor UI as a click-to-play player. **This is a debug aid only — the audio is never sent to the LLM.**

### Write side (HAL)

In `_process_job`, every inference that clears the per-label confidence gate is written to disk by `_persist_wav()` before it lands in the buffer:

- **Directory:** `SPEECH_EMOTION_AUDIO_DIR` (config in `hal/config.py:563`, env `HAL_SPEECH_EMOTION_AUDIO_DIR`), default `<tempdir>/hal-speech-emotion` (i.e. `/tmp/hal-speech-emotion`). Created with `os.makedirs(exist_ok=True)` at init; if creation fails the directory is disabled and every POST carries an empty `audio` field (graceful degradation — SER keeps working).
- **Contents:** the **pre-prefilter** WAV, i.e. what the mic captured, not the trimmed buffer sent to the model.
- **Filename:** `<ms>_<user>_<label>.wav`, where `<ms>` is the inference timestamp in milliseconds and `<user>`/`<label>` are sanitized to `[a-zA-Z0-9_-]` (anything else collapsed to `_`). That sanitization is what lets the Go handler serve these files by basename alone.
- **Flush selection:** when a user's flush emits the dominant non-neutral label, it attaches the **latest** clip among the dominant-label inferences — `max(dom_inferences, key=lambda i: i.ts).audio_path` — as the `audio` field in the POST.

### Serve side (OS server)

The OS server exposes the clip to the Flow Monitor UI **only** via `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`, `handler.go:821`). It serves the WAV by **basename** (the full path never leaves the device) from one of:

```
/var/lib/hal/speech-emotion
/tmp/hal-speech-emotion
```

The basename is validated (`.wav` suffix, no `/`, `\`, or `..`) before serving. On `PostSensingEvent`, the raw `audio` path is mapped to a servable URL by `audioURLForPath` (`handler.go:808`) and attached to the Monitor `sensing_input` event detail; the Monitor turn item renders it as a clickable audio player. The raw path is never exposed to the UI, and the `audio` field is never concatenated into the outgoing chat text.

### Known limitation: no cleanup

Every qualifying inference's WAV is persisted — **including neutral clips that are structurally guaranteed to be dropped at flush**, and non-dominant clips that never become an event. There is **no automatic cleanup** of the audio directory. On the default path that directory is on **tmpfs, i.e. RAM**, so on a long-running device it must be pruned by external housekeeping. Tracked as [known-issues #1](speech/speech-emotion-known-issues.md#1--debug-wav-directory-grows-without-bound-on-tmpfs) and [#2](speech/speech-emotion-known-issues.md#2--neutral-results-are-persisted-and-buffered-then-always-discarded).

---

## Debug Tracer (SER-DEBUG)

The audio persistence above only keeps clips that *passed* the confidence gate, and it keeps nothing about **why** a clip was dropped. The SER-DEBUG tracer (`hal/drivers/voice/speech_emotion/debug_tracer.py`) fills that gap: with `HAL_SER_DEBUG=true` it writes one directory per utterance and per flush decision, holding the audio, every metric behind the decision, and a per-stage latency/memory profile. It mirrors the SPEAKER-DEBUG tracer in `speaker_recognizer.py` — same env-knob shape, same dir-naming convention, same "never raises, never changes behavior" contract — and like it, **it is dev-only and meant to be deleted before a final deploy** (`grep -rn "SER-DEBUG" hal/drivers/voice/speech_emotion/`).

**Off by default.** The switch is read once at import; restart HAL after changing it. When off, every trace call is an attribute lookup and a `nullcontext` — no timers, no RSS sampler, no audio decode.

### Layout

```
<root>/recognize/<ts>_<label>_<confidence>/   one utterance: submit → prefilter → HTTP → verdict
<root>/recognize/<ts>_FAIL-<reason>/          dropped before any label existed
<root>/emit/<ts>_<label>_<confidence>/        one flush decision for one user
<root>/emit/<ts>_FAIL-<reason>/               flush produced nothing to send
```

`<root>` defaults to `speech_emotion_logs/` beside `debug_tracer.py` (git-ignored); if the source tree is read-only, it falls back to `<tempdir>/hal-ser-debug` rather than silently disabling. `<ts>` is `YYYYMMDD-HHMMSS-ffffff`; the naming matches the speaker and facial-emotion logs, so all three read the same way.

A dir named by a label does **not** mean the event fired — a low-confidence drop and a dedup drop both have a real classification, and naming them by it is what makes the trace worth opening. `result.json`'s `verdict` field carries the actual outcome (`buffered` / `emitted` / `dropped`, with `drop_reason`).

Each dir holds:

| File | Contents |
|------|----------|
| `input.wav` | the WAV as submitted — what the mic session produced, before the prefilter |
| `prefiltered.wav` | the trimmed WAV actually uploaded; absent when the prefilter dropped the sample |
| `result.json` | the whole decision: user, input audio stats, engine config (URL, encryption, whether Silero loaded), every prefilter threshold **and** the metrics measured against them, the HTTP exchange, label/confidence/threshold/bucket, and the verdict |
| `profile.json` | per-stage wall-clock / CPU / RSS — `recognize` → `prefilter` (→ `decode_wav`, `rms_trim`, `silero_vad`, `encode_wav`), `encode_b64`, `api_request`, `api_decode`, plus `persist_wav` and `send_to_sensing` |

One utterance produces **one** directory even though the code that knows the audio, the HTTP exchange and the buffering verdict lives in three different places: the service opens a thread-local trace, the engine fills it in as the audio moves through, and the service closes it. Submit-time drops happen on the caller's thread before that trace exists, so they are written as one-shot dirs with no profile — nothing was run to profile.

### Reading a profile

`profile.json` answers "where did the time and memory go". RSS is **sampled** on a background thread (~20 ms) and each stage reports the peak inside its own window, because endpoint-only sampling reports `0.0` for a stage that allocates and frees within its window. Read `rss_peak_delta_mb` (what the stage cost at its worst); `rss_end_delta_mb` is what it *kept* and is legitimately negative when the allocator hands pages back. `cpu_pct > 100%` means more than one core; `api_request` near 0% is correct — it is waiting on the network. Both RSS and `cpu_ms` are process-wide, so a busy concurrent HAL thread inflates them; read one stage as an upper bound and prefer the shape across several calls. A one-line summary is also logged per call (`SER-DEBUG profile [recognize]: total=… recognize.prefilter.silero_vad=…ms/…%cpu/+…MB …`).

### Env knobs

| Env var | Default | Effect |
|---------|---------|--------|
| `HAL_SER_DEBUG` | `false` | `true` to enable (covers the trace **and** the profile). Read once at import — restart HAL after changing |
| `HAL_SER_DEBUG_DIR` | `speech_emotion_logs/` beside `debug_tracer.py` | Output root |
| `HAL_SER_DEBUG_MAX_ENTRIES` | `1000` | Per-kind dir cap, oldest pruned; `0` = unbounded |

The knobs are read straight from `os.environ` inside `debug_tracer.py`, **not** through `hal/config.py` — the whole block stays removable without touching config. See also the [pipeline debug playbook](speech/speech-emotion-pipeline.md#debug-playbook).

---

## Polarity Buckets

Bucketing mirrors the facial pipeline so `(user, bucket)` dedup keys are interpretable across modalities. The buckets are:

| Bucket | Labels |
|--------|--------|
| `positive` | happy, surprised |
| `negative` | angry, disgusted, fearful, sad |
| `other` | neutral, other, `<unk>` (these are **dropped before bucketing** — see anti-spam guard #5, so this bucket is unreachable in practice) |

Why bucket-level dedup, not label-level: emotion2vec on short utterances flips between sad/fearful/angry within the same affective state. Per-label dedup would over-deliver. Per-bucket dedup collapses within-bucket noise (sad ↔ fearful ↔ angry) into one negative event per window; cross-bucket flips (sad → happy) still fire as a genuine mood change.

---

## Anti-Spam Guards

Layered, matched to the facial emotion processor:

| # | Stage | Drop condition |
|---|-------|----------------|
| 1 | `submit()` | `wav_bytes` empty / `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` |
| 2 | `submit()` | `user` is empty after normalize (no subject to attribute emotion to — mirrors face `current_user==""`) |
| 3 | engine | **prefilter** — RMS trim/voiced/ratio gate, then Silero VAD (see above) |
| 4 | worker | `confidence < CONFIDENCE_THRESHOLD_BY_LABEL[label]` (per-label gate, see Configuration) |
| 5 | flush | label is `neutral` / `other` / `<unk>` |
| 6 | flush | `(user, bucket)` was sent less than `SPEECH_EMOTION_DEDUP_WINDOW_S` seconds ago |

Each bucket keeps its own independent TTL entry in `_last_sent_by_key`. Sending a positive event does NOT reset the negative window (and vice versa). Same semantics as facial emotion.

The TTL map is persisted to a boot-scoped sidecar (`/tmp/hal-ser-state.json`, `hal/dedup_sidecar.py`) so a HAL service restart restores the dedup window instead of re-firing the last-known emotion on the first flush after a deploy/OTA. A full device reboot starts fresh (tmpfs + kernel `boot_id` check). Facial emotion uses the same mechanism with its own file (`/tmp/hal-emotion-state.json`, `drivers/sensing/perceptions/processors/emotion.py:35`).

Three **further** rate limits apply server-side, after SER's own dedup: `speech_emotion.detected` is an `ambientFloorTypes` member (`SensingTurnFloorSeconds`, default 120 s), it expires from the runtime's pending queue after 60 s, and it coalesces to the last occurrence of its type (`runtimes/hermes/events.go:124-150`). An event that survives the edge can still be discarded before it reaches the agent.

> **Guard ordering is the pipeline's main performance problem.** Guards 5 and 6 — the two with real suppression power — run *after* the cloud call, the local Silero pass and the disk write have already been paid for. See [known-issues #7](speech/speech-emotion-known-issues.md#7--the-suppression-gates-all-sit-downstream-of-the-only-expensive-step).

---

## Configuration

All knobs live in `hal/config.py` as `SPEECH_EMOTION_*`, overridable via env vars. Defaults mirror `EMOTION_*` so the two modalities behave identically out of the box.

| Constant | Env var | Default | Purpose |
|----------|---------|---------|---------|
| `SPEECH_EMOTION_ENABLED` | `HAL_SPEECH_EMOTION_ENABLED` | `true` | Master kill switch |
| `SPEECH_EMOTION_FLUSH_S` | `HAL_SPEECH_EMOTION_FLUSH_S` | `10.0` | Buffer drain cadence |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | `HAL_SPEECH_EMOTION_DEDUP_WINDOW_S` | `300.0` | TTL for `(user, bucket)` |
| `SPEECH_EMOTION_MIN_AUDIO_S` | `HAL_SPEECH_EMOTION_MIN_AUDIO_S` | `3.0` | Min utterance length |
| `SPEECH_EMOTION_API_TIMEOUT_S` | `HAL_SPEECH_EMOTION_API_TIMEOUT_S` | `15` | **Dead config** — never passed to the engine, which always uses the hardcoded 15 s. See [known-issues #3](speech/speech-emotion-known-issues.md#3--speech_emotion_api_timeout_s-is-dead-config) |
| `SPEECH_EMOTION_AUDIO_DIR` | `HAL_SPEECH_EMOTION_AUDIO_DIR` | `/tmp/hal-speech-emotion` | Debug WAV dir; `""` disables persistence |
| `DL_SER_ENDPOINT` | `DL_SER_ENDPOINT` | `/hal/api/dl/ser/recognize` | Path suffix on `DL_BACKEND_URL` |
| `SPEECH_EMOTION_API_URL` | — | derived | `DL_BACKEND_URL` + `DL_SER_ENDPOINT` |
| `SPEECH_EMOTION_API_KEY` | — | mirrors `DL_API_KEY` | Sent as `X-API-Key` |
| `DL_ENCRYPTION_ENABLED` | `HAL_DL_ENCRYPTION` | `true` | Wrap request/response bodies |
| `DL_ENCRYPTION_REQUIRED` | `HAL_DL_ENCRYPTION_REQUIRED` | `false` | Hard-fail instead of plaintext fallback |
| `SPEAKER_MIN_AUDIO_S` | `HAL_SPEAKER_MIN_AUDIO_S` | `0.8` | Also gates the SER WAV build; inert here, since `submit()`'s 3.0 s floor is higher |

Label vocabulary, bucket map, prefilter thresholds, and **per-label confidence thresholds** are declared in `hal/drivers/voice/speech_emotion/constants.py` (not env-overridable — touching these requires a code change). The threshold dict:

```python
# constants.py:38
CONFIDENCE_THRESHOLD_BY_LABEL: dict[str, float] = {
    SpeechEmotionLabel.HAPPY:     0.5,
    SpeechEmotionLabel.SURPRISED: 0.6,
    SpeechEmotionLabel.SAD:       0.7,
    SpeechEmotionLabel.ANGRY:     0.6,
    SpeechEmotionLabel.FEARFUL:   0.6,
    SpeechEmotionLabel.DISGUSTED: 0.6,
}
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5  # fallback for unlisted labels
```

Negative emotions get higher gates to avoid false-positive alarms; `happy` is loosest because positive misfires are cheap; `sad` carries the highest bar of all (the code states the values, not the reasoning). Lookup goes through `utils.threshold_for(label)`, which falls back to `DEFAULT_CONFIDENCE_THRESHOLD` for any unmapped label — including `neutral`, which therefore clears the gate at 0.5 and is only discarded later at flush.

Also not configurable: `DEFAULT_QUEUE_MAXSIZE = 32`, the hedge strings, the 3-attempt OS-server retry, and the sidecar path `/tmp/hal-ser-state.json`.

---

## Integration Point

Called from `dispatch_turn` (`_internal/turn_dispatch.py:157`), which itself runs from `VoiceService._stream_session`'s `finally` block. Speaker recognize runs **once** per session — in the speaker-ID prepass at `voice_service.py:1407` — and its result feeds both the OS-server-message decoration and the SER `user` field:

```python
# voice_service.py finally, after finalize_session:

# 1. Speaker-ID prepass — the ONE recognize this turn. Runs here because the
#    voiceprint needs the whole utterance, so this is the earliest possible point.
if combined:
    _final_text, _ = self._decorator.classify_wake_word(combined)
    turn_identity = self._decorator.identify_and_decorate(_final_text, audio_buffer)

# 2. Dispatch — reuses turn_identity, never recognizes twice
dispatch_turn(self._decorator, self._sensing_sender, combined,
              audio_buffer, ser_audio_buffer, rt, identity=turn_identity)
```

```python
# turn_dispatch.py
user = UNKNOWN_USER_LABEL
if combined:
    final_msg, se_user, _ = identity          # or identify_and_decorate(...) if None
    user = se_user if se_user else UNKNOWN_USER_LABEL
    sensing_sender.send(...)                  # routed by rt.handled / rt.delegated

# Submit SER — uses the UNTRIMMED snapshot so laughter / sighs survive.
decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
```

`submit_speech_emotion_from_session` (`speaker_decorate.py:343`) is a thin submitter with no embedded speaker call:

```python
session_audio = self._session_wav_for_ser(audio_buffer)
if session_audio is None:
    return                                          # buffer empty or < SPEAKER_MIN_AUDIO_S
wav_bytes, duration_s = session_audio
self._speech_emotion.submit(user=user, wav_bytes=wav_bytes, duration_s=duration_s)
```

The whole call is wrapped in `try/except` — a SER failure can never kill a voice turn.

### SER user attribution

| Speaker ID outcome | `user` passed to `submit()` |
|--------------------|-----------------------------|
| `match=True` with enrolled name | Speaker label (e.g. `alice`) |
| `match=False` / below threshold (API OK, no `error`) | `unknown` — set directly by `identify_and_decorate` (`speaker_decorate.py:317`) |
| Recognize skipped or failed (`se_user` is `None`) | `unknown` — substituted at `turn_dispatch.py:105` |
| No transcript at all (`if combined:` skipped) | `unknown` — the `turn_dispatch.py:94` init value survives |
| Wake-word gate rejected the turn | `unknown` — the default parameter at `voice_service.py:1555` |

SER is never invoked from inside `identify_and_decorate`.

> Those five cases are **indistinguishable on the wire**, and the OS server reads `current_user` as "who is in front of the device" (`handler.go:155` calls `mood.SetCurrentUser`). An SER event whose speaker-ID came back `unknown` therefore overwrites a good face-derived identity. See [known-issues #6](speech/speech-emotion-known-issues.md#6--current_user-clobbers-the-device-wide-identity).

### When SER is not submitted

- The device declares no `audio` capability — voice people-perception (speaker-ID + SER) is gated on the mic, so `SpeakerDecorator` is constructed with `enable_people_perception=False` and the SER service never initializes (`speaker_decorate.py:117`). (This is the `audio` capability, not `presence`: a mic is all SER needs. Facial emotion in the sensing loop stays `presence`-gated.)
- `SPEECH_EMOTION_ENABLED=false`, or `SpeechEmotionService` not `available` (no `DL_BACKEND_URL`)
- `ser_audio_buffer` empty or shorter than `SPEAKER_MIN_AUDIO_S` (gates `_session_wav_for_ser`)
- `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` (gates `submit()` itself — default 3.0 s, the binding floor)
- `submit()` drops (queue full, empty `user` after normalize)

`wav_bytes` is built from `ser_audio_buffer` — the **untrimmed** snapshot taken before `finalize_session` trims trailing silence off the speaker-recognition copy. That is deliberate: laughter, sighs and trailing "hmm"s carry affect but are not words, and the speaker-recognition trim would cut them.

Init is one-shot per process in `SpeakerDecorator.__init__`, mirroring the speaker recognizer pattern: the instance is created once, threads start only when the engine reports `available`.

### Cost note: speaker recognize is shared across paths

Speaker recognize fires **once** per mic session. The single `(final_msg, se_user, display)` result is reused by:

1. The OS server POST — when STT had a transcript.
2. The SER submit — always.

This is why the ordering is: finalize → wake-word classification → speaker-ID prepass (once) → dispatch (OS server POST → SER submit). `dispatch_turn` accepts the prepass result as its `identity` argument and only recomputes it when `None`; SER never issues its own `/embed` request.

---

## Failure Modes

| Failure | Effect | Recovery |
|---------|--------|----------|
| `DL_BACKEND_URL` not configured | `recognizer.available` is False, threads never start, `submit()` is a no-op (`SERVICE IDLE` at boot) | Set `llm_base_url` in the OS server config |
| perception-service down (connection refused) | Worker logs warning, sample dropped, no retry on this leg | Next utterance retries automatically |
| perception-service returns non-200 / non-JSON / no `label` | Worker logs warning, sample dropped | Same as above |
| Encryption required but no public key | `RuntimeError` at construction → caught in `_init_speech_emotion` → service is `None` | Fix `DL_PUBLIC_KEY_URL`/`DL_PUBLIC_KEY_FILE`, or unset `HAL_DL_ENCRYPTION_REQUIRED` |
| Silero model missing / ORT broken | Prefilter falls back to a **stricter** RMS bar (3.0 s voiced) | Restore `resources/silero_vad.onnx`; check the one-time load warning |
| Prefilter rejects the clip | Sample dropped before the cloud call, with the driving metrics logged | Expected for TV/music/sparse audio; tune `constants.py:87-135` if legitimate speech is being cut |
| Worker queue full | `submit()` logs warning, drops the **new** job | Indicates backend overload; see [known-issues #4](speech/speech-emotion-known-issues.md#4--queue-drops-the-newest-job-and-never-ages-out-stale-ones) |
| OS server sensing endpoint down | 3 retries with 2 s back-off, then sample dropped | Buffer continues filling for next flush |
| `duration_s < MIN_AUDIO_S` | Dropped in `submit()` with a log line | Expected — short utterances aren't worth classifying |
| Audio dir `mkdir` fails | Persistence disabled for the process; every POST carries an empty `audio` | Check permissions on `HAL_SPEECH_EMOTION_AUDIO_DIR` |

Nothing here blocks the STT path or speaker recognition — SER failures are silent at the user level and visible only in the HAL server log. For the full drop-reason → log-line index and a step-by-step debug playbook, see [docs/speech/speech-emotion-pipeline.md](speech/speech-emotion-pipeline.md#every-drop-reason-in-order).

### Manual verification

Two scripts (manual, not unit tests — they need a mic and a reachable backend):

```bash
python -m hal.test.test_speech_emotion_engine     # cloud leg only
python -m hal.test.test_speech_emotion_service    # full pipeline + mock OS server on :5000
```

---

## Diagnostic Snapshot

`SpeechEmotionService.to_dict()` returns a runtime snapshot for introspection endpoints:

```json
{
  "type": "speech_emotion",
  "available": true,
  "buffered_users": 2,
  "dedup_keys": 3,
  "queue_size": 0,
  "last_flush_ts": 1715587812.41
}
```

`dedup_keys` is bounded by (enrolled users seen) × 2 buckets. It over-reports while idle, because expired keys are only pruned on flush ticks that have buffered work.

---

## Relationship to Other Pipelines

| Pipeline | Modality | Trigger | Event type | Same skill consumes? |
|----------|----------|---------|------------|----------------------|
| Facial emotion (`drivers/sensing/perceptions/processors/emotion.py`) | Camera frame → face crop | Every face seen | `emotion.detected` | yes — `user-emotion-detection/SKILL.md` |
| **Speech emotion (this doc)** | Mic → end-of-session WAV | Every mic session with ≥ 3.0 s of audio that passes the prefilter — **independent of STT transcript** | `speech_emotion.detected` | yes — same `user-emotion-detection/SKILL.md` (router accepts both prefixes) |
| Mood synthesis (Mood skill) | — | Any emotion signal | mood `signal` / `decision` rows | — |
| Sound (`sound.py` perception) | Mic RMS | Loud noise | `sound` | dog-bark escalation, separate skill |

Speech emotion shares the polarity vocabulary with facial emotion deliberately. The OS server's sensing handler tags incoming events with `[speech_emotion]` (vs `[emotion]` for face) in `system/lib/sensingmsg/sensingmsg.go:78`, pre-fetches the same `[emotion_context: …]` block via `skillcontext.BuildEmotionContext` (`sensingmsg.go:116-122`, one branch serving both types), and routes to `user-emotion-detection/SKILL.md`. The label-to-mood map covers both vocabularies (`Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`); the only modality-specific behavior in the skill is `source:"voice"` vs `source:"camera"` on the mood signal log row. Music-suggestion cooldown is shared across modalities so voice cannot bypass a recent camera-driven suggestion, and vice versa.

`[speech_emotion]` is also in every runtime's `ackSkipPrefixes` (`runtimes/*/emotion_ack.go`), so these turns do **not** drive the "thinking" face — they frequently resolve to `NO_REPLY`, which would otherwise leave the face stuck.

---

## See also

- [docs/speech/speech-emotion-pipeline.md](speech/speech-emotion-pipeline.md) — stage-by-stage data flow, complete threshold tables, drop-reason index, debug playbook.
- [docs/speech/speech-emotion-known-issues.md](speech/speech-emotion-known-issues.md) — open defects and performance problems.
- [docs/speech/README.md](speech/README.md) — the whole speech service (STT, TTS, speaker recognition, realtime).
- [docs/speech/cloud-models.md](speech/cloud-models.md) — the emotion2vec endpoint and its ONNX/TensorRT serving chain.
- [docs/perception-service.md](perception-service.md) — the cloud DL inference service, load balancer, encryption.
- [docs/face-emotion/README.md](face-emotion/README.md) — the camera-side twin.
