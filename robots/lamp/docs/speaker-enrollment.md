# Speaker Voice Enrollment — Technical Spec

**Status: IMPLEMENTED** (2026-04)

## Overview

Lamp identifies who is speaking via **WeSpeaker ResNet293** by default (256-dim embedding, ONNX Runtime; server-selectable via `AUDIO_EMBEDDER__MODEL`). When a speaker is not recognized, HAL saves the audio and optionally nudges the AI agent to enroll the voice. Enrollment is **self-service only** — each person enrolls their own voice.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  HAL (Python, port 5001)                                         │
│                                                                     │
│  VoiceService._stream_session()                                     │
│    ├─ STT transcript ready                                          │
│    ├─ identify_and_decorate(transcript)                             │
│    │   ├─ audio_buffer → WAV → on-device preprocess (VAD gate)     │
│    │   │   └─ Mono→Resample→[HPF]→[NR]→VAD→[STOI]→RMS; reject clip  │
│    │   ├─ POST /audio-recognizer/embed  (preprocess=false)         │
│    │   │   └─ WeSpeaker ONNX → 256-dim L2-normalized (embed only)  │
│    │   ├─ Per-chunk voting vs enrolled embeddings                   │
│    │   ├─ Match ≥ 0.5 raw cos → "Speaker - Name: transcript"        │
│    │   └─ No match → _format_unknown_speaker_message()              │
│    │       ├─ _should_request_speaker_enroll() gate                 │
│    │       │   ├─ ≥ 10 words in transcript                          │
│    │       │   └─ ≥ 2s audio duration                               │
│    │       ├─ PASS → "Unknown Speaker: ... (audio save at <path>,   │
│    │       │          auto enroll ...)"                              │
│    │       └─ FAIL → "Unknown Speaker: ..." (no enroll instruction) │
│    └─ POST /api/sensing/event → Lamp (Go)                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Lamp (Go, port 5000)                                               │
│                                                                     │
│  Two paths (both call domain.AppendEnrollNudge):                    │
│                                                                     │
│  1. Direct path (handler.go)                                        │
│     └─ Agent idle → send immediately to OpenClaw                    │
│                                                                     │
│  2. Drain path (service.go)                                         │
│     └─ Agent busy → queue → replay when idle                        │
│                                                                     │
│  AppendEnrollNudge(msg) — domain/voice.go:                          │
│    ├─ Check: contains "Unknown Speaker:" + "audio save at"          │
│    ├─ Cooldown: skip if < 5 min since last nudge                    │
│    └─ Append: "[REQUIRED: Follow speaker-recognizer/SKILL.md ...]"  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  OpenClaw Agent                                                     │
│                                                                     │
│  speaker-recognizer/SKILL.md                                        │
│    ├─ Detects self-introduction ("I'm X", "my name is X")           │
│    ├─ curl POST /speaker/enroll with wav_path + name                │
│    ├─ Two-turn: ask "Who are you?" → enroll with both paths         │
│    └─ Confirm: "Nice to meet you, Name!"                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Anti-Spam Gates

Four layers prevent the agent from repeatedly asking "who are you?":

| Layer | Where | Gate | Purpose |
|-------|-------|------|---------|
| **Audio duration** | HAL `_internal/speaker_decorate.py` | `duration_s < SPEAKER_MIN_AUDIO_S` (0.8s) | Skip recognition entirely for very short audio |
| **Enroll instruction** | HAL `_should_request_speaker_enroll()` | `≥ 10 words AND ≥ 2s audio` | Don't append full enroll instruction for short utterances (short variant with multi-turn combine hint is still sent) |
| **Lamp-side nudge cooldown** | Lamp `domain/voice.go` | `5 min since last nudge` | Don't inject SKILL.md instruction more than once per 5 min |
| **Per-voiceprint nudge cooldown** | HAL `_internal/speaker_decorate.py` | `30 min per voiceprint_hash` (`HAL_ENROLL_NUDGE_COOLDOWN_S`) | Don't repeat "ask user's name" for the same unknown voice cluster; plain `Unknown Speaker:` message sent instead |

## Model & Embedding

| Property | Value |
|----------|-------|
| Model | WeSpeaker ResNet293-LM by default (`AUDIO_EMBEDDER__MODEL=resnet293`; alternatives `resnet34`, `ecapa-tdnn1024`, `campplus`) |
| Embedding dim | 256 |
| Runtime | ONNX Runtime (CPU) on perception-service (RunPod) |
| Endpoint | `POST {DL_BACKEND_URL}/hal/api/dl/audio-recognizer/embed` (`DL_SPEAKER_ENDPOINT`; the legacy `/lelamp/` prefix is still mapped by nginx) |
| Auth | `X-API-Key` header |
| Timeout | 15s |

### Recognition Algorithm

1. Audio → **on-device** preprocess on HAL (`Mono → Resample → [HighPass] → [NoiseReduce] → VAD → [STOI] → RMS`). Clips that fail the VAD/STOI/quality gate are rejected locally (treated as "unknown") and **never uploaded**.
2. Cleaned WAV → `POST /audio-recognizer/embed` with `preprocess=false` **and `use_sliding_window=true`**; the server skips its own preprocessing and slides overlapping windows to return per-chunk embeddings `[M, 256]` (a clip ≤ ~10 s stays a single window)
3. Cosine similarity against all enrolled speaker embeddings
4. Per-chunk voting: each chunk votes for its closest match
5. Winner = most votes (tiebreak by average confidence)
6. `confidence ≥ SPEAKER_MATCH_COS` (0.5 raw cosine) → match; else unknown

> **Enroll differs:** enrollment calls the same endpoint with **`use_sliding_window=false`**, so the server embeds the **whole** reference utterance in a single shot (one `[256]` vector, no windowing/mean) — stored as one row per WAV in the speaker bank. Recognition then votes its windowed query chunks against those single-shot enrollment vectors (both live in the same L2-normalized space).

### Auto-extend admission (extended tier)

A recognized turn may earn a slot in the speaker's **extended** tier, so the bank
picks up the acoustics the room actually produces rather than only the enrollment
recording. Being recognized is **not** enough. Six gates run in this order, and
the first one that fails rejects the **whole turn** — nothing is salvaged,
sliced, or partially admitted.

| # | Gate | Rejects when | Why |
|---|------|--------------|-----|
| 1 | **Unanimity** | any chunk voted for a different speaker | The turn is stored as ONE vector over the whole utterance, so a turn identified by *majority* vote can hold another speaker in the losing chunks and that blend becomes a permanent retrieval row — with nothing in the stored vector to reveal it |
| 2 | **Weakest chunk** | the worst winning chunk is below `HAL_SPEAKER_EXTEND_MIN_CHUNK_COS` | Unanimity alone proves nothing: with a **single enrolled speaker** there is one column, so every chunk votes for them by default even at cos 0.2. This is the gate that catches the common case — a guest in the room who is not enrolled |
| 3 | **Anchor-carried** | the enrollment rows did not themselves carry the match (`HAL_SPEAKER_EXTEND_MIN_ANCHOR_COS`) | A match the **extended** tier carried is evidence about a previous guess, not about the person. Admitting on it would let the tier vouch for its own growth, so one bad sample could breed more. Anchoring on enrollment is what makes contamination non-replicating |
| 4 | **Duration** | post-VAD speech is under `SPEAKER_EXTEND_MIN_DURATION_SEC` | Too little speaker information to be worth a permanent slot. Measured on the **cleaned** waveform, not the raw turn — the caller joins an entire mic session (up to 30 s) into one WAV, so on a quiet turn most of the file is silence |
| 5 | **Margin** | the winner beats the best **non-winning** speaker by less than `SPEAKER_EXTEND_MIN_MARGIN_COS` | A near-tie between two enrolled users must never write audio into either one's bank |
| 6 | **Diversity** | max cosine to what is already stored exceeds `SPEAKER_DIVERSITY_COS` | The sample duplicates one we already hold |

Gates 1 and 2 are **no-ops below ~10 s of post-VAD speech**, where the server
returns a single chunk: unanimity is 1-of-1, and the weakest chunk *is* the
turn's own confidence, which the match threshold already cleared. They only ever
fire on long turns — precisely the ones that can hold two people.

**Expect the extend rate on long turns to drop.** That is the intended effect. If
it drops to zero, check gate 2 against real recordings before loosening anything:
a zero rate more likely means `SPEAKER_MATCH_COS` is wrong than that the gate is.

Every admitted sample writes a `.json` beside its WAV recording which gates it
passed and under which thresholds. Nothing reads it at runtime — it exists so a
later threshold change can ask which samples the previous rule admitted. Samples
written before this existed carry no such record, and there is no way to
reconstruct one: the per-chunk votes were never persisted. They age out through
the `SPEAKER_MAX_EXTENDED_SAMPLES` cap.

The sample's embedding is produced by the **same single-shot call enroll uses**
(`use_sliding_window=false`, whole utterance, no windowing or mean), so anchor
and extended rows are the same kind of vector and re-embedding a stored
`ext_*.wav` during a model migration reproduces its own sidecar. Samples written
before this carry the mean of recognition's sliding-window chunks and no
`embedding_mode` key; the next migration rewrites them single-shot.

> The enroll path is deliberately **exempt**: audio claimed from a voice cluster
> during enrollment is committed on the user's own say-so, so it bypasses these
> gates entirely.

### Audio preprocessing (on-device)

The filter/VAD/normalize pipeline that used to run inside perception-service now runs on HAL, next to the mic — the same processors in the same order, ported to `hal/drivers/voice/speaker_recognizer/audio_processors/` (mirrors `AudioProcessorFactory` in perception-service). This keeps rejected audio off the network and puts the gate decision on the device.

- **Default chain**: `MonoConverter → Resampler(16k) → VoiceActivityFilter(TEN-VAD) → SpeechIntelligibilityFilter(0.70) → RMSNormalizer(0.1)`. `HighPassFilter` and `NoiseReducer` exist but are **off by default** (same as perception).
- **VAD gate** (TEN-VAD via the vendored `hal/drivers/voice/ten_vad_lite/`): trims leading/trailing non-voice and rejects a clip when it removes all speech, the remaining audio is `< 0.5s`, or the voice ratio is `< 0.25`. A rejected clip raises `PreprocessRejected` → HAL returns "unknown" for recognize and skips the sample for enroll — exactly the behavior it had when perception returned HTTP 400.
- **Why TEN-VAD, not silero**: this stage ran the torch `silero-vad` package until it was swapped for TEN-VAD's ~300 KB FP32 ONNX model, run on numpy + onnxruntime (both already HAL deps — the original model only; no quantized build ships). Same class, same constructor, same reject reasons, so the rest of the pipeline is untouched. It takes **torch off this path**: +43 MB for import + model load instead of +170 MB, ~27x faster cold start, and onnxruntime has aarch64 wheels where upstream TEN-VAD's prebuilt `libten_vad` has no Linux-arm64 build at all. TEN-VAD is 16 kHz-only, which the `Resampler` upstream already guarantees.
- **False-positive gates** (no silero equivalent): a **speaker-band** gate keeps only VAD frames inside the clip's own pitch band, and a **level** gate drops frames more than 20 dB below the clip's own speech level. They matter because the filter keeps *first speech sample to last*, so one late false positive on a door or a tap drags all the silence before it into the clip. Together they raise the share of the kept span that is really speech from ~0.67 to ~0.79, at a real recall cost (0.98 → 0.76) — the right trade for a recogniser, where a clean 2 s beats a dirty 6 s. They assume **one dominant speaker per clip**: a background talker is removed (usually desirable, it would pollute the embedding), but so would a genuine quieter second speaker. Because they also cut non-speech from *inside* the span, the voice ratio drops mechanically — which is why the min voice ratio moved from `0.4` to `0.25`. Set `HAL_SPEAKER_PROC_VAD_SPEAKER_BAND=false` and `HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB=` (empty) for plain TEN-VAD.
- **STOI gate** (`SpeechIntelligibilityFilter`, reference-free SQUIM-OBJECTIVE STOI) — **off by default**, `HAL_SPEAKER_PROC_ENABLE_STOI=true` to enable: runs **after VAD, before RMS**. Scores the trimmed clip in 5 s chunks and **mean**-aggregates, then rejects when the mean STOI is `< 0.70` (a NaN chunk from silence also rejects), raising `PreprocessRejected(reason="low_intelligibility")` → the same audio-level reject path as VAD (recognize → "unknown", enroll → skip the sample, keeping existing on-disk samples). The ONNX estimator (~20 MB, downloaded on first use from the CDN into `/root/local/models/squimm_stoi.onnx` — see `audio_processors/model_store.py`, same convention as the pose/faceid weights — onnxruntime CPU with the mem-arena off) loads once as a lazy singleton alongside TEN-VAD and only runs after VAD passes — at most once per utterance. If the weight can't be resolved (unreachable CDN / unknown filename) the gate is skipped with a warning (no crash).
- **Server flag**: HAL sends `preprocess=false`; perception's `/embed` is embed-only and now defaults to `preprocess=false` too (HAL is the only caller). A caller that uploads raw audio can pass `preprocess=true` to have the server clean it.
- **Consistency**: enroll and recognize share this one pipeline, so enrollments made after the move stay self-consistent. Voices enrolled under the **old server-side** pipeline should be re-enrolled if match quality drops.

### Embedding-model version tracking & migration

A stored embedding is only comparable to a query embedding produced by the **same** server model. If the perception-service embedding model is swapped, every previously-stored vector silently becomes meaningless to compare against — cosine similarity still returns a number, so the failure is a **wrong match**, not an error. HAL guards against this by stamping each profile with the model identity and re-embedding when it changes. Because every enrollment WAV is retained on disk, this is an automatic background job — no user has to re-record.

- **Model identity**: perception's `/audio-recognizer/embed` response returns `embed_model_version` (and `/health` the same value as `audio_embedder_version`) — `<model-name>:<sha256(weights)[:12]>`, computed once when the model loads. `<model-name>` is the `AUDIO_EMBEDDER__MODEL` config value (`resnet293` / `resnet34` / `campplus` / `ecapa-tdnn1024`), e.g. `resnet293:1a2b3c4d5e6f`. Hashing the weights file catches even a **same-dimension checkpoint swap** that the `embedding_dim` check would miss. Only the model is fingerprinted; the on-device preprocessing config is deliberately **not** part of it.
- **On enroll**: HAL always takes the freshest version seen from that enroll's `/embed` calls and writes it to the voice `metadata.json` as `embed_model_version` (mirrored into the registry).
- **On recognize**: after embedding the query (which refreshes the known server version), HAL compares each enrolled profile's stored version against it. Profiles whose version **differs** are **excluded from matching this turn** (so they read as **"unknown"** rather than wrong-matching against old-model vectors, and a dim change can't crash the match), and a one-shot **background** re-embed migration is kicked — single-flight, on a daemon thread, so the recognize turn itself never waits for the re-embed. Fresh profiles match normally in the same call; excluded ones return to normal automatically once the background migration re-embeds them.
- **On HAL restart**: a background thread polls `/health` for the current `audio_embedder_version` (a few retries to cover server boot), cheaply scans profile metadata for staleness **before** loading the heavy preprocessing model, and migrates any stale profiles — so recognition is correct from the first turn.
- **Migration (re-embed)**: for each stale profile, HAL re-embeds **every** retained sample — both the anchor (`sample_*.wav`) and extended (`extended_*.wav`) tiers — under the new model (same `_prepare_wav_for_embedding` → `/embed` path as enroll) and **atomically** overwrites each sample's embedding **sidecar** (`.npy`, temp file + rename); a sample the gate now rejects has its sidecar dropped so no old-model vector survives a same-dimension checkpoint swap. It then updates `embed_model_version` / `embedding_dim` / `updated_at` and invalidates the bank cache. Guarded so only one migration runs at a time, and a concurrent enroll of the **same** profile is now **serialized** against it — enroll's disk-commit takes the same per-user lock. That lock matters because migration rewrites this profile's sidecars (and `metadata.json`) while a concurrent enroll of the same user may be writing the same files — without it the two disk-commits could interleave and leave an inconsistent sample set; `metadata.json`, the only cross-writer file, has its sample counts re-derived from disk on read. Only the disk commit is guarded — the embedding network calls on both sides run lock-free — so at worst an enroll waits for that one profile's re-embed, never the whole migration batch. The commit runs only **after** every sample embedded, so a mid-migration server outage (`EmbeddingAPIUnavailableError`) **halts** cleanly with the profile fully on the old model and is retried on the next recognize or restart.
- **Un-migratable profiles are left stale, never deleted.** A profile HAL cannot re-embed — because it has **no source WAVs** (a legacy `embedding.npy`-only enrollment, or WAVs deleted), or because **every retained WAV is rejected by today's gate** — is left **stale** (excluded from matching) until the person re-enrolls, not removed. An all-rejected profile is almost always the gate config tightening (the WAVs passed it at enroll under the old config), which is reversible, so the profile re-migrates automatically once a sample passes the gate again; and a stored `embedding.npy`/WAV may be the only copy of the enrollment, so a version bump must never destroy it. A stale profile is harmless — it is filtered out of matching and costs only a cheap per-turn check.

### Enrollment Quality

1. Each WAV sample → on-device preprocess (as above) → embedding via perception-service (`preprocess=false`, `use_sliding_window=false` → one whole-utterance vector per sample)
2. Filter by consistency threshold `0.7` (cosine similarity between samples)
3. Aggregate remaining embeddings via weighted average
4. Store L2-normalized vector at `/root/local/users/{name}/voice/embedding.npy`

### Voice Cluster Tracking (`voiceprint_hash`)

Every unknown voice is locally clustered so the server can say "this is the same unknown speaker we heard 3 minutes ago" without needing any backend support. Lets the agent combine multiple short utterances into one enroll call.

1. After embedding the query audio, the recognizer pools per-chunk embeddings into a single L2-normalized vector.
2. Compare against every stored cluster **row**, taking each cluster's best row (raw cosine). A cluster holds several rows — not one averaged centroid — capped by `SPEAKER_MAX_CLUSTER_SAMPLES` (default `3`) and admitted through the same diversity gate the extended tier uses.
3. Match ≥ `SPEAKER_MATCH_COS` (default `0.5` raw — the **same** threshold as known-speaker matching; there is no separate stranger threshold) → reuse existing label `voice_N` and, if the utterance adds something new, append it as another row.
4. No match → allocate new label `voice_{counter}`, append its row to on-disk state.
5. Cap at `HAL_MAX_VOICE_STRANGERS` (default `50`) **clusters** — the oldest whole cluster is evicted and its on-disk directory is deleted with it. (Eviction used to drop only the in-memory centroid, leaving the audio behind forever.)
6. The assigned hash is:
   - returned on the recognize response as `voiceprint_hash: "voice_N"` (null for known speakers)
   - surfaced in the nudge message as `[voice:voice_N]` tag so the skill can correlate turns
   - used to subdir-group the saved WAV (see Storage)

**Model change wipes the cluster store.** Stranger centroids are only comparable to a query from the **same** embedding model — so unlike enrolled profiles (which are *re-embedded* from retained WAVs), the whole stranger store is **wiped** when the model changes. The store is stamped with the model version it was built under (`voice_strangers/version.txt`); before any compare, HAL keeps the store **only when it can prove same-model provenance** — the live server version is known, the store's stamp **equals** it, **and** the stored dim equals the query dim. Anything else — a **missing** stamp, a **different** stamp, or a **different** dim — cannot prove the centroids came from the current model, so HAL drops the in-memory centroids, deletes the `embeds.npy`/`labels.npy` and every on-disk `voice_N/` WAV dir, and re-stamps. (An unstamped store is **never assumed** current: a same-dim checkpoint swap under a different model would otherwise slip through.) When the server reports **no** version at all, HAL falls back to a dim-only guard. `_stranger_counter` is kept **monotonic** so a freshly minted `voice_N` never collides with a leftover dir. Wiping (not re-embedding) is deliberate: strangers are anonymous and short-lived, so re-embedding throwaway clusters isn't worth the network cost.

**Trailing-silence trim**: before the WAV goes to the embedding API, the speaker-ID buffer is truncated at the last speech frame + 200 ms tail. Without this a 3-second utterance ends up as ~5.5 s with ~45% silence, diluting the embedding. Only affects the speaker-ID path — STT still receives the full stream.

## Configuration

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Match threshold | 0.5 | `SPEAKER_MATCH_COS` | Min **raw** cosine for speaker match; also pairs clips within a multi-sample enroll batch (was `SPEAKER_MATCH_THRESHOLD` = 0.75 scaled; `raw = 2 × scaled − 1`) |
| Diversity | 0.7 | `SPEAKER_DIVERSITY_COS` | Above this a turn duplicates a stored sample → not kept. Redundancy, not identity — must stay above the match threshold |
| Max extended samples | 3 | `SPEAKER_MAX_EXTENDED_SAMPLES` | Auto-collected samples per user. Safety cap: retrieval is max-over-rows, so extra rows lift every speaker's score |
| Max cluster samples | 3 | `SPEAKER_MAX_CLUSTER_SAMPLES` | Rows kept per unknown-voice cluster |
| Extend min duration | 2.0s | `SPEAKER_EXTEND_MIN_DURATION_SEC` | A turn needs this much **post-VAD speech** to earn an extended slot. Measured on the cleaned waveform, so silence in a long mic session does not count |
| Extend min margin | 0.05 | `SPEAKER_EXTEND_MIN_MARGIN_COS` | ...and must beat the best **non-winning** speaker by this much. `inf` when nobody else is enrolled — there is genuinely no runner-up, which is why gate 3 exists |
| Extend unanimity | on | `HAL_SPEAKER_EXTEND_REQUIRE_UNANIMOUS_CHUNKS` | Every chunk must have voted for the winner. Off below ~10 s of speech (one chunk) |
| Extend min chunk cosine | _(= match)_ | `HAL_SPEAKER_EXTEND_MIN_CHUNK_COS` | The **weakest** winning chunk must clear this. Defaults to `SPEAKER_MATCH_COS`, already tighter than the turn-level gate, which only checks the average |
| Extend min anchor cosine | _(= match)_ | `HAL_SPEAKER_EXTEND_MIN_ANCHOR_COS` | The enrollment rows must have carried the match. Defaults to `SPEAKER_MATCH_COS`, i.e. exactly "the anchors alone would have recognized this speaker" |
| API timeout | 15s | `SPEAKER_EMBEDDING_API_TIMEOUT_S` | HTTP timeout for embedding API |
| Min audio for recognition | 0.8s | `HAL_SPEAKER_MIN_AUDIO_S` | Skip recognition below this |
| Min words for enroll nudge | 10 | Hardcoded in `_should_request_speaker_enroll()` | Transcript word count gate |
| Min duration for enroll nudge | 2.0s | Hardcoded in `_should_request_speaker_enroll()` | Audio duration gate |
| Lamp nudge cooldown | 5 min | Hardcoded in `domain/voice.go` | Don't re-inject SKILL instruction globally |
| Per-voiceprint nudge cooldown | 30 min | `HAL_ENROLL_NUDGE_COOLDOWN_S` | Don't re-ask name for same voiceprint cluster |
| Voice stranger match threshold | _(shared)_ | `SPEAKER_MATCH_COS` | Reuses the known-speaker match threshold to cluster an unknown voice into an existing `voice_N` — no separate knob |
| Max voice strangers | 50 | `HAL_MAX_VOICE_STRANGERS` | Cluster **count** cap; oldest whole cluster evicted, its audio dir deleted with it |
| Voice strangers dir | `/root/local/voice_strangers` | `HAL_VOICE_STRANGERS_DIR` | Persist cluster embeddings (survives reboot) |
| Speaker recognition enabled | true | `HAL_SPEAKER_RECOGNITION_ENABLED` | Master toggle (default on; gated on the `audio` capability) |

### On-device preprocessing knobs

Mirror perception's `AudioProcessorSetting` defaults; override via env (all prefixed `HAL_SPEAKER_PROC_`).

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Target sample rate | 16000 | `HAL_SPEAKER_PROC_TARGET_SR` | Resampler target |
| Mono | on | `HAL_SPEAKER_PROC_ENABLE_MONO` | Downmix to mono |
| Resample | on | `HAL_SPEAKER_PROC_ENABLE_RESAMPLE` | Resample to target SR |
| High-pass | off | `HAL_SPEAKER_PROC_ENABLE_HIGH_PASS` / `..._HIGH_PASS_CUTOFF_HZ` (80.0) | Butterworth HPF |
| Noise reduce | off | `HAL_SPEAKER_PROC_ENABLE_NOISE_REDUCE` / `..._NOISE_STATIONARY` | `noisereduce` (lazy import) |
| VAD | on | `HAL_SPEAKER_PROC_ENABLE_VAD` | TEN-VAD gate |
| VAD min duration | 0.5s | `HAL_SPEAKER_PROC_VAD_MIN_DURATION_SEC` | Reject if stripped audio shorter |
| VAD min voice ratio | 0.25 | `HAL_SPEAKER_PROC_VAD_MIN_VOICE_RATIO` | Reject if voice fraction lower. Was 0.4 under silero — the false-positive gates split segments, which lowers the ratio |
| VAD speech-prob threshold | 0.5 | `HAL_SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD` | TEN-VAD onset threshold (offset = −0.15); higher trims trailing/leading silence more. Was 0.6 under silero; TEN-VAD's measured operating point is 0.45–0.5, and the gates below now do that trimming |
| VAD speaker band | on | `HAL_SPEAKER_PROC_VAD_SPEAKER_BAND` | Keep only VAD frames in the clip's own pitch band |
| VAD max level drop | 20.0 dB | `HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB` | Drop VAD frames this far below the clip's own speech level; **empty string disables** |
| STOI gate | **off** | `HAL_SPEAKER_PROC_ENABLE_STOI` | SQUIM-OBJECTIVE intelligibility gate (after VAD, before RMS). Off by default: it rejected real speakers often enough that turns lost their speaker entirely — worse than a lower-confidence identification. Turn on in noisy rooms. |
| STOI model path | `/root/local/models/squimm_stoi.onnx` | `HAL_SPEAKER_PROC_STOI_MODEL_PATH` | ONNX estimator (~20 MB), downloaded from CDN on first use; gate skipped if unresolvable |
| STOI threshold | 0.70 | `HAL_SPEAKER_PROC_STOI_THRESHOLD` | Reject if mean STOI below this |
| STOI chunk | 5.0s | `HAL_SPEAKER_PROC_STOI_CHUNK_SEC` | Chunk length scored, then mean-aggregated |
| RMS normalize | on | `HAL_SPEAKER_PROC_ENABLE_RMS_NORMALIZE` / `..._RMS_TARGET` (0.1) | Fixed-loudness normalize |

### Debug tracing (temporary)

`speaker_recognizer.py` carries a self-contained diagnostic tracer, tagged `SPEAKER-DEBUG` throughout the file, for tuning recognition thresholds on real audio. **It is OFF by default (production-safe) — set `HAL_SPEAKER_DEBUG=true` to enable it during development**, and it is meant to be deleted entirely before a final deploy. `grep -n "SPEAKER-DEBUG"` finds every line belonging to it; no other module or config file is involved.

Each `recognize()` / `enroll()` call writes one directory:

```
<root>/recognize/<ts>_<class>_<confidence>/     class = enrolled name | stranger-<N> | unknown
<root>/recognize/<ts>_FAIL-<reason>/            no-voice | low-voice | low-stoi | too-short | server-error | …
<root>/enroll/<ts>_<norm>_<cohesion>/           cohesion = mean sim of kept samples to the centroid
<root>/enroll/<ts>_FAIL-<reason>/
```

holding `input.wav` (raw) plus `preprocessed.wav` (post VAD/STOI/RMS — the audio actually uploaded) / `sample_new_NN.wav`, the embeddings as `.npy`, `result.json`, and `profile.json` (latency + memory only — see below). A recognize records a `preprocessing` block (cleaned duration/RMS, the STOI score the clip passed with, and the threshold it cleared) so you can tell a "bad audio" miss from a "wrong speaker" miss; a clip killed by the gate instead files a `FAIL-<reason>` dir whose `preprocessing_reject` holds the structured reason and its measurements.

A gate reject also dumps **the audio the chain had produced when the gate said no** — `after_<stage>.wav`, named for the last stage that ran, with a `preprocessing_partial` block (that stage, the stage that rejected, duration/RMS/sample-rate). This is the point of it: a `FAIL-low-stoi` dir used to hold only the raw `input.wav`, so there was no way to hear the **TEN-VAD output** the STOI gate actually scored — the clip the rejection is about. Now that lands as `after_ten_vad.wav`. A VAD reject similarly files `after_resample.wav` (whatever ran before it). The enroll `FAIL-no-valid-samples` dir carries the same per sample: `sample_NN_input.wav` + `sample_NN_after_<stage>.wav`, with each sample's structured gate reason under `per_sample_errors`.

For a recognize the JSON carries the **full** decision breakdown — not just the top-3 `candidates` the API returns, but `speaker_summary` (votes + mean/max similarity for *every* enrolled speaker, including 0-vote losers) and `per_chunk_scores` (each chunk vs every speaker, plus which speaker that chunk voted for). The same matrix is saved as `chunk_scores.npy` (`[chunks × speakers]`, columns in `enrolled_speakers` order). Unknown speakers also record the stranger-cluster match score and which cluster was closest.

#### Latency, CPU + memory profile

Each trace dir also holds a **`profile.json`** — per-stage wall-clock, CPU and memory for the call, so a slow or memory-hungry turn can be attributed to a specific stage instead of the pipeline as a whole. It is kept in its own file rather than mixed into `result.json`, which is already dense with the recognition decision: timings and the decision breakdown are read for different reasons and each would bury the other. It rides on the existing tracer otherwise — same dir, same switch, no extra env var, on only when `HAL_SPEAKER_DEBUG=true`. A one-line summary is also logged (`SPEAKER-DEBUG profile [recognize]: total=… preprocess.stoi_gate=…ms/…%cpu/+…MB …`).

Stages form a **tree**: a stage opened inside another becomes its child, so containment is structural rather than a naming convention. Summing the top level gives the call total without double-counting, and each node's `self_ms` is its own time minus its children's — the parent's glue, not its children's work.

```
decode_input                base64/file read + WAV normalize to 16 kHz mono
preprocess                  the whole on-device chain
├─ decode_wav / encode_wav    WAV bytes ↔ float32 waveform, plus the base64 wrap
├─ processor_init             lazy build/start — first call loads TEN-VAD + ONNX STOI
├─ mono / resample / high_pass / noise_reduce / rms_normalize
├─ ten_vad                  ← the TEN-VAD stage (named `silero_vad` in traces predating the swap)
└─ stoi_gate                ← the STOI intelligibility gate
embed_api                   the embedding call
├─ request                  ← the HTTP round-trip itself
└─ decode                     response parse + L2 normalize
load_enrolled / match_vote / stranger_cluster / save_input_wav
```

**Memory.** RSS is sampled on a background thread (~20 ms) for the life of the call, and each stage reports the **peak inside its own window**. Endpoint-only sampling — read RSS at entry, read again at exit — is wrong for this pipeline: RSS moves only when the allocator asks the OS for pages or returns them, so a stage that allocates and frees inside its own window reports `0.00`, and a stage that runs while an earlier allocation is released reports a *negative* cost. Three numbers are therefore kept per stage:

| Field | Means |
|-------|-------|
| **`rss_peak_delta_mb`** | peak-during-stage minus RSS at entry — **the memory number to read**. Survives allocate-then-free. For a repeated stage it is the worst single occurrence, not a sum |
| `rss_end_delta_mb` | exit minus entry — what the stage *kept*. Legitimately negative when pages go back to the OS |
| `rss_peak_mb` / `rss_after_mb` | absolute high-water / final RSS during the stage |

**CPU.** `cpu_ms` is process CPU time, which is what catches the ONNX/torch intra-op thread pools — the work that makes `stoi_gate` expensive. `cpu_pct` is `cpu_ms/ms×100`, so **>100% means it used more than one core** and **~0% means it was blocked, not working** (`embed_api.request` should sit near zero — it is waiting on the network). `thread_cpu_ms` is the calling thread alone, so the gap between it and `cpu_ms` is roughly what the pools did. The top level adds `cpu_count` so >100% is interpretable.

Other notes worth knowing:

- **A stage that rejects is still measured.** A clip killed by VAD or STOI files a `FAIL-…` dir whose `profile.json` shows what those gates cost before they said no — a reject pays for the same inference as a pass.
- **Enroll aggregates.** It runs preprocess + embed once per sample, so shared stages sum their `ms`, with `calls` and `ms_max` alongside (both omitted when a stage ran exactly once).
- **RSS and CPU are process-wide.** A concurrent HAL thread allocating or burning CPU during a stage lands in that stage's numbers — no RSS-based method escapes this. Read a single stage's memory as an upper bound and prefer the shape across several calls.
- **`rss_source` changes what the numbers mean.** `psutil` / `statm` (on-device Linux) are current RSS — the accurate case. `rusage` — the macOS-without-psutil fallback — is a high-water mark that cannot fall, so `rss_end_delta_mb` overstates there.
- Memory is process RSS, not a Python-heap measure: the ONNX TEN-VAD and STOI sessions allocate outside the heap, where `tracemalloc` would see nothing.

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Debug tracing | **off** | `HAL_SPEAKER_DEBUG` | Set `true` to enable (covers the trace **and** the profile). Read once at construction — restart HAL after changing |
| Output root | `speaker_logs/` next to `speaker_recognizer.py` | `HAL_SPEAKER_DEBUG_DIR` | Falls back to a temp dir if the source tree is read-only (device deploy) |
| Max entries | 1000 | `HAL_SPEAKER_DEBUG_MAX_ENTRIES` | Per-kind directory cap, oldest pruned; `0` = unbounded |

The default output dir is git-ignored — never commit trace output. The tracer swallows all of its own errors, so a failing trace can never break recognition.

## Storage

```
/root/local/users/{name}/
  metadata.json                      # Shared identity (telegram, display_name)
  voice/
    embedding.npy                    # L2-normalized aggregated vector [256]
    metadata.json                    # num_samples, dim, timestamps,
                                     #   embed_model_version
    sample_{origin}_{ts}_{uuid}.wav  # Individual enrollment samples (16kHz mono)

/tmp/hal-unknown-voice/
  incoming_{ts}_{uuid}.wav           # Known-speaker audio (flat)
  voice_{N}/
    incoming_{ts}_{uuid}.wav         # Unknown audio — grouped by voiceprint cluster

/root/local/voice_strangers/
  embeds.npy                         # Stranger cluster centroids [N, 256] (deleted while store is wiped)
  labels.npy                         # Cluster labels ["voice_1", "voice_2", ...] (deleted while store is wiped)
  counter.npy                        # Monotonic counter for next new label (survives a wipe)
  version.txt                        # Embed-model version the centroids were built under; mismatch → wipe
```

## API Endpoints (HAL, port 5001)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/speaker/enroll` | Enroll voice from wav_paths + name |
| `POST` | `/speaker/record-enroll` | Record from the device mic (`arecord`, `duration_sec` 1–60, default 15) then enroll the capture |
| `POST` | `/speaker/recognize` | Recognize speaker from wav_path |
| `POST` | `/speaker/identity` | Link Telegram identity to existing profile |
| `POST` | `/speaker/remove` | Remove voice profile by name |
| `POST` | `/speaker/reset` | Remove all voice profiles |
| `GET`  | `/speaker/list` | List enrolled speakers |

### Error contract

`/speaker/enroll` distinguishes two failure classes:

| HTTP | When | Skill behavior |
|------|------|----------------|
| `400` | Audio-level reject (too short, silent, VAD found no speech, mean STOI below threshold → `low_intelligibility`, perception-service returned 4xx) | Ask user to re-record / speak more clearly |
| `503` | Embedding service unreachable (network, 5xx, malformed response) | Tell user to try again shortly — nothing on disk was modified |

`/speaker/recognize` never fails with 5xx for embedding outages — it returns `200` with `{name: "unknown", error: "<reason>"}` so the skill can gracefully degrade. Only input-level problems (missing WAV, bad base64) return `400`.

### Mic ownership during record-enroll

ALSA capture is exclusive — only one process may hold the mic. `/speaker/record-enroll` therefore stops the voice pipeline, records with `arecord`, and restarts the pipeline from its own `finally` block.

Any other path that starts the pipeline while that recording is in flight steals the capture device and **both** sides fail with `audio open error: Device or resource busy`: the enroll returns `500` and the voice loop dies on the same error. So every caller goes through `state.start_voice_service(reason)`, which refuses (and logs the reason) while `state._enrolling` is set. The single exception is record-enroll's own restore — it owns the stop and runs after the flag is cleared.

## Key Code Locations

| Component | File | Function/Struct |
|-----------|------|-----------------|
| STT → speaker ID | `hal/drivers/voice/_internal/speaker_decorate.py` | `identify_and_decorate()` |
| Enroll gate | `hal/drivers/voice/_internal/speaker_decorate.py` | `_should_request_speaker_enroll()` |
| Message formatting | `hal/drivers/voice/_internal/speaker_decorate.py` | `_format_unknown_speaker_message()` |
| Speaker recognizer | `hal/drivers/voice/speaker_recognizer/speaker_recognizer.py` | `SpeakerRecognizer` |
| Mic-ownership gate | `hal/app_state.py` | `start_voice_service()` |
| Record + enroll route | `hal/routes/speaker.py` | `speaker_record_enroll()` |
| Nudge injection + cooldown | `system/domain/voice.go` | `AppendEnrollNudge()` |
| Direct event path | `system/server/sensing/delivery/http/handler.go` | `PostEvent()` |
| Drain/replay path | `runtimes/openclaw/service_events.go` | `drainPendingEvents()` |
| Agent skill | `skills/speaker-recognizer/SKILL.md` | — |
| Embedding model | `integrations/perception-service/src/core/perception/audio/predictors/` (`resnet34.py`, `resnet293.py`, `ecapa_tdnn.py`, `campplus.py`) | `ResNet293Embedder` (default), `ResNet34Embedder`, `EcapaTdnn1024Embedder`, `CamPPlusEmbedder` — selected via env `AUDIO_EMBEDDER__MODEL` (`resnet293` \| `resnet34` \| `ecapa-tdnn1024` \| `campplus`) |
| Embedding endpoint | `integrations/perception-service/src/dlserver/routes/audio.py` | `embed_audio()` |
| Config | `hal/config.py` | `SPEAKER_*` constants |

## Message Flow Examples

### Short utterance (blocked)
```
User says: "hey" (2 words, 0.9s audio)
→ HAL: skip recognition (< SPEAKER_MIN_AUDIO_S)
→ Message: "hey" (no prefix, no enroll instruction)
```

### Medium utterance (recognized but no enroll nudge)
```
User says: "turn on the lights please" (5 words, 3s audio)
→ HAL: recognize → unknown, _should_request_speaker_enroll(5 words, 3s) = false
→ Message: "Unknown Speaker: turn on the lights please"
→ Lamp: no "audio save at" in message → AppendEnrollNudge returns unchanged
→ Agent: responds normally, doesn't ask who user is
```

### Multi-turn combine (same voice cluster)
```
User turn 1: "nice to meet you today. Okay." (5 words)
→ HAL: recognize → unknown, voiceprint_hash=voice_5
→ WAV moved to /tmp/hal-unknown-voice/voice_5/incoming_A.wav
→ Message: "Unknown Speaker: [voice:voice_5] nice to meet you today. Okay. (audio saved at ..._A.wav. Note: audio is too short for single enrollment. If prior turns tagged the same voice_5, combine their saved paths with this one...)"
→ Agent: asks "Could you tell me your name?"

User turn 2: "I'm Alex." (2 words)
→ HAL: voiceprint_hash=voice_5 (same cluster, sim=0.75)
→ WAV moved to /tmp/hal-unknown-voice/voice_5/incoming_B.wav
→ Message: "Unknown Speaker: [voice:voice_5] I'm Alex. (audio saved at ..._B.wav...)"
→ Agent: scans prior turns for same [voice:voice_5] tag → finds path A
→ Agent: POST /speaker/enroll with wav_paths=[path_A, path_B], name="Alex"
→ Agent: "Nice to meet you, Alex!"
```

### Long utterance (full enroll flow)
```
User says: "Hi my name is Leo and I just got home from work..." (30 words, 8s audio)
→ HAL: recognize → unknown, _should_request_speaker_enroll(30 words, 8s) = true
→ Message: "Unknown Speaker: Hi my name is Leo... (audio save at /tmp/hal-unknown-voice/incoming_xxx.wav, auto enroll...)"
→ Lamp: AppendEnrollNudge → cooldown OK → append "[REQUIRED: Follow speaker-recognizer/SKILL.md...]"
→ Agent: detects "my name is Leo" → POST /speaker/enroll → "Nice to meet you, Leo!"
```

### Cooldown (blocked)
```
Same unknown speaker, 2 minutes later:
→ HAL: _should_request_speaker_enroll = true (long enough)
→ Message has "audio save at"
→ Lamp: AppendEnrollNudge → cooldown NOT elapsed (< 5 min) → skip instruction
→ Agent: sees "Unknown Speaker: ..." without SKILL instruction → responds normally
```
