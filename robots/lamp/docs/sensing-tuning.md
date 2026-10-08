# Sensing Threshold Tuning Guide

How to tune Lamp's sensing thresholds on real hardware.
All constants live in `hal/config.py` and `hal/drivers/voice/_internal/config.py`.

## View Logs

SSH into the Pi, then:

```bash
# HAL log (motion, sound, light events all here)
tail -f /var/log/hal/server.log

# Lamp log (confirms event received + forwarded to OpenClaw)
journalctl -fu lamp -f
```

When an event fires you will see two lines — one in each log:

```
# lelamp log
INFO lelamp.service.sensing.sensing_service: [sensing] motion: Small movement detected...

# lamp log
[sensing] received motion event → forwarding to OpenClaw
```

---

## Motion Detection (Activity Recognition)

`MotionPerception` runs Kinetics action recognition (via perception-service) and emits a
`motion.activity` event with the recognized activity labels.

**File:** `hal/config.py`

```python
MOTION_CONFIDENCE_THRESHOLD = 0.3    # min action-recognition confidence to buffer a label
MOTION_FLUSH_S = 10.0                # buffer drain cadence — at most one flush per 10s
MOTION_EVENT_COOLDOWN_S = 900.0      # same-class heartbeat floor between emissions (15 min)
MOTION_TRANSITION_MIN_GAP_S = 60.0   # min gap for the class-transition cooldown bypass
```

**Emission gates (in order, `motion.py`):**

1. **Flush cadence** — buffered detections are drained at most once per `MOTION_FLUSH_S`.
2. **Presence gate** — no event unless presence == PRESENT.
3. **Global cooldown** — no `motion.activity` more than once per `MOTION_EVENT_COOLDOWN_S`
   while the **coarse activity class** (the `ACTIVITY_GROUP` set: sedentary/eat/drink/…)
   stays the same. Same-class raw-label flips (`writing → drawing`) stay floored — that's
   noise. Bypassed by: a **class transition** (`computer → eat` is real information,
   emitted as soon as `MOTION_TRANSITION_MIN_GAP_S` has passed, which stops a flickering
   detection from re-opening every-flush spam), a posture nudge (already time-gated by the
   pose window), and a user change (a new user/session sees a fresh event immediately).
4. **Per-label dedup** — even when cooldown is cleared, the same `(user, label-set)`
   within a 5-min window is dropped. Noisy Kinetics labels flip the set often, so the
   global cooldown above is the dominant gate.

**How to read the log:**

```
INFO hal...motion: [motion] raw actions in window: ['writing', 'typing']
INFO hal...motion: [motion] flushing: Activity detected: writing.
INFO hal...motion: [motion] cooldown drop: ... (last event 42.1s ago < 900s floor, class unchanged)
INFO hal...motion: [motion] transition bypass: ['sedentary'] → ['eat'] (last event 312.4s ago)
```

**Tuning:**

| Symptom | Fix |
|---------|-----|
| `motion.activity` fires constantly (every ~10s) | Increase `MOTION_EVENT_COOLDOWN_S` — this is the same-class floor |
| Repeated events from a blinking detection (drink in/out of frame) | Increase `MOTION_TRANSITION_MIN_GAP_S` (60 → 120+) |
| Activity not picked up at all | Decrease `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.2) |
| Spurious activity labels | Increase `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.4) |
| Reaction lags a real activity change | Decrease `MOTION_FLUSH_S` (10 → 5) and/or `MOTION_TRANSITION_MIN_GAP_S` — class changes already bypass the cooldown |

---

## Sound Detection (Sensing)

**File:** `hal/config.py`

```python
SOUND_RMS_THRESHOLD = 8000   # RMS level to trigger "loud noise" event (env HAL_SOUND_RMS_THRESHOLD)
SOUND_SAMPLE_DURATION_S = 0.5 # sample window length
```

**How to read the log:**
The event message includes the actual RMS level:

```
INFO lelamp.service.sensing.sensing_service: [sensing] sound: Loud noise detected (level: 9521)
```

Watch the `level` value during normal ambient conditions vs. when you clap/speak loudly.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Normal speech doesn't trigger event | Decrease `SOUND_RMS_THRESHOLD` (8000 → 4000) |
| Triggers on fan noise / AC hum | Increase `SOUND_RMS_THRESHOLD` (8000 → 12000) |


---

## Voice Wake Word (VAD)

**File:** `hal/drivers/voice/_internal/config.py` (all env-tunable)

```python
HAL_VAD_THRESHOLD = 3500        # RMS to trigger speech detection (default 3500)
HAL_SILENCE_TIMEOUT = 2.5       # fallback: stop STT session after this much silence (s)
HAL_ENDPOINT_SILENCE_S = 0.8    # short clock used once STT emitted a final segment (s)
HAL_SPEECH_HOLDOFF = 0.2        # min speech duration before opening STT — rejects short clicks (s)
HAL_PRE_ROLL_FRAMES = 8         # rolling lookback frames kept BEFORE VAD trigger (8 × 64ms = 512ms)
HAL_WEBRTCVAD_ENABLED = false   # secondary gate, recommended true for low-threshold setups
HAL_SILERO_ENABLED = false      # tertiary gate (ONNX); webrtcvad usually enough
```

These are the code defaults. The shipped lamp profile (`robots/lamp/rootfs/opt/hal/.env`) overrides the two silence clocks to `HAL_SILENCE_TIMEOUT=1.0` and `HAL_ENDPOINT_SILENCE_S=0.6` — end of turn after 1.0 s of silence, or 0.6 s after an STT final; Smart Turn can still hold a hesitation (see *Two silence clocks* in `docs/realtime-voice.md`).

**How pre-roll works:** Every mic frame goes into a rolling `deque(maxlen=PRE_ROLL_FRAMES)` regardless of VAD state. When VAD finally triggers, the pre-trigger history (frames that fell under `RMS_THRESHOLD` — e.g. quiet stop consonants like "b", "k", "t", "p") gets prepended to the audio stream sent to STT. This eliminates the need for the user to say "Uhm..." as a warmup before their actual phrase.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| First syllable clipped (STT hears "ật đèn" instead of "bật đèn") | Increase `HAL_PRE_ROLL_FRAMES` (8 → 12) or decrease `HAL_VAD_THRESHOLD` (3500 → 1500) |
| Wake word not picked up reliably | Decrease `HAL_VAD_THRESHOLD` (3500 → 1500) + enable `HAL_WEBRTCVAD_ENABLED=true` as safety net |
| Lamp starts listening from ambient noise | Increase `HAL_VAD_THRESHOLD` and/or enable `HAL_WEBRTCVAD_ENABLED=true` |
| Lamp cuts off before you finish speaking | Increase `HAL_ENDPOINT_SILENCE_S` (the clock that normally ends a turn), then `HAL_SILENCE_TIMEOUT` |
| Lamp answers ~2s after you stop speaking | Decrease `HAL_ENDPOINT_SILENCE_S` (0.8 → 0.5) |
| Stale audio from previous turn bleeds into next session | Already mitigated: `lookback.clear()` fires after each session closes |
| Lamp repeats its own TTS back to OpenClaw (echo loop) | Decrease `ECHO_SIMILARITY_THRESHOLD` (0.55 → 0.45) |

---

## Light Level Detection

**File:** `hal/config.py`

```python
LIGHT_LEVEL_INTERVAL_S = 300.0  # check every 5 minutes (env HAL_LIGHT_LEVEL_INTERVAL_S)
LIGHT_CHANGE_THRESHOLD = 100    # min brightness change (0–255) to trigger event (env HAL_LIGHT_CHANGE_THRESHOLD)
```

**How to read the log:**

```
INFO lelamp.service.sensing.sensing_service: [sensing] light.level: Ambient light decreased significantly (level: 45/255, change: -120)
```

**Tuning:**

| Symptom | Fix |
|---------|-----|
| No event when lights are turned on/off | Decrease `LIGHT_CHANGE_THRESHOLD` (100 → 50) |
| Too sensitive (triggers from lamp dimming slowly) | Increase `LIGHT_CHANGE_THRESHOLD` (100 → 150) |
| Events too frequent | Increase `LIGHT_LEVEL_INTERVAL_S` |

---

## Face Detection

**File:** `hal/config.py`

```python
FACE_HEIGHT_RATIO_THRESHOLD = 0.10  # Skip faces shorter than 10% of frame height
FACE_MAX_TRUNCATION = 0.05          # Skip faces with >5% of their bbox off-frame
FACE_MIN_SHARPNESS = 100.0          # Skip faces too motion-blurred to identify
FACE_STRANGER_MIN_TICKS = 2         # Sightings before an unknown face earns an id
FACE_STRANGER_CORROBORATION_S = 6.0 # How long a pending candidate stays countable
HAL_FACE_LANDMARK_CONF_THRESHOLD = 0.99  # Skip crops the face mesh isn't sure about
FACE_MATCH_THRESHOLD = 0.40         # Bar for a match carried by the enrolled uploads
FACE_EXTENDED_THRESHOLD = 0.45      # Bar for a match carried by the extended bank alone
FACE_STRANGER_THRESHOLD = 0.45      # Bar for matching an already-known stranger_N
FACE_EXTEND_MIN_ENROLL_SIM = 0.45   # Bar the uploads must clear to auto-capture a view
FACE_COOLDOWN_S = 10.0              # Min seconds between face presence events
FACE_OWNER_FORGET_S = 3600.0        # Re-fire presence after N seconds without seeing owner
FACE_STRANGER_FORGET_S = 1800.0     # Same for strangers
FACE_STRANGER_GAZE_TICKS = 3        # Stranger greeting: face-ID ticks in the gaze vote
FACE_STRANGER_GAZE_MIN_FACING = 2   # Stranger greeting: ticks facing the lamp needed
FACE_STRANGER_GAZE_WINDOW_S = 10.0  # Stranger greeting: max age of a gaze vote
```

The height ratio threshold filters out faces that are **too small** relative to the frame — typically distant people or false positives where the face crop is too low-resolution for reliable recognition. Faces whose bounding-box height is under the threshold fraction of the frame height are skipped, before classification.

**Why height and not area.** Area falls off as 1/d² while a linear dimension falls off as 1/d, so an area gate is twice as sensitive for the same change in reach — extending range from 0.8 m to 1.5 m needs a 3.8× change in an area threshold but only a 1.95× change in a height threshold. More importantly, yaw (turning the head, the common case) compresses the bbox **width** while leaving height intact, so an area gate rejected angled faces harder than frontal ones at the same distance — working against the extended-set feature that exists to learn those angled views.

**Truncation.** `FACE_MAX_TRUNCATION` is a separate gate for faces clipped by a frame edge, applied after the height gate and before classification. A clipped face is not a smaller face — it is a face missing features. SCRFD still returns a plausible box (the clipped edge simply runs off-frame, e.g. `[573, -34, 710, 121]`), and the landmark mesh confidently invents the part it cannot see. Measured on a lamp on 2026-09-03: a face cut off above the eyebrows produced eye landmarks hallucinated onto the cheeks at a landmark confidence of **0.90**, an embedding sharing **0.007** similarity with that person's own enrollment photo, and a FRIEND verdict carried entirely by the auto-captured extended bank.

The landmark-in-bbox check inside the aligner does **not** catch this: it clamps the bbox to the frame before comparing, so a landmark can never be "outside" on the very edge that clipped the face. The gate measures the fraction of the bbox **area** falling outside the frame. Rejected faces also never reach `extend_candidates`, so a cut-off view can never be auto-added to a user's extended enrollment set.

**Why 0.05.** Replaying 496 logged frames from a lamp (2026-09-04): every well-recognised frame sat at 0% overflow (median similarity to the enrollment photo 0.66), the 0.1–3% band at 0.56, and the **5–10% band collapsed to 0.32**. One frame clipped by 6.1% minted a spurious `stranger_N` identity, and three later frames then matched *that* identity — so a single clipped frame produced four misidentifications. Dropping the threshold from 0.10 to 0.05 removes all four; the cost is three frames out of 492 that recognise correctly today and are instead skipped.

**What it does not catch.** The gate only sees clipping the detector admits to by returning an off-frame box (e.g. `y1 = -34`). SCRFD sometimes clamps to the edge instead (`y1 = 0` exactly), which measures 0% overflow and passes even though the face is genuinely cut. Treating "bbox touches the frame edge" as clipped was measured and rejected: it would drop 25 frames to catch 2, because 23 edge-touching frames are recognised correctly.

**Blur.** `FACE_MIN_SHARPNESS` is the third geometry-independent gate: the variance of the Laplacian of the **aligned 112×112 crop**, below which the detection is dropped before any decision. Motion blur — the lamp panning, or the person moving — destroys a face without making it smaller or clipping it, so neither gate above sees it.

What comes out of a smeared crop is a near-random embedding that resembles nothing, and *"resembles nothing"* is the **new-stranger** branch: a blurred frame of the enrolled user mints a `stranger_N` identity for him. That is what happened on a lamp on 2026-09-04 — one frame captured mid-servo-sweep, similarity 0.10 to the user's own enrolment photo, sitting between two clean recognitions at 0.63 and 0.76.

Measure it on the **aligned** crop, never the detector crop: the latter varies in size frame to frame, and Laplacian variance scales with resolution, so its values are not comparable between frames. Cost is ~0.06 ms on an array that already exists.

| gate | frames dropped |
|------|------|
| lapvar < 70 | 1.6% |
| **lapvar < 100** | **5.7%** |
| lapvar < 130 | 11.0% |

**Why 100, and not just enough to clear the offending frame.** The cost is asymmetric. Nearly every dropped frame would have been recognised correctly — and losing one is silent, because the camera re-samples every `HAL_SENSING_INTERVAL` (2 s) and `current_user()` holds the person for `FACE_OWNER_FORGET_S` (1 h). A false stranger event on the user's own face is not silent.

Note what the gate does and does not do: it separates **usable from unusable frames**, not the enrolled user from a stranger. A blurred frame of a genuine stranger is dropped too, which costs that person a few seconds' delay before an identity is minted — they also produce a frame every 2 s and mint from the next sharp one. Laplacian variance scales with lighting and contrast, so this number is calibrated to this camera; re-check it against `FAIL-blurred` folders in the face debug log if the room or optics change.

**Landmark confidence.** `HAL_FACE_LANDMARK_CONF_THRESHOLD` (in `model_store.py`) is the third gate, applied inside the aligner: a detection whose face-mesh confidence falls below it is dropped and never embedded. **Read this number in the scale the model emits** — the score saturates, with a median of exactly 1.000 and a minimum of 0.613 over 990 logged frames, so the useful range is the last hundredth. The old default of 0.60 did not mean "fairly strict"; it meant the gate never fired, not once.

It screens out a crop that is facial but carries no identity — SCRFD firing on an **ear** at close range is the case seen in the field. 26 such frames appeared in one 40-minute session at ~0.0 similarity to the user's own enrollment photo; one minted a `stranger_N` identity and the other 25 then matched it. `landmark_score` separates those from real faces at AUC 0.98:

| gate | frames reaching recognition | recognised | spurious identities minted |
|------|------|------|------|
| 0.60 (old) | 980 | 94.6% | 1 |
| 0.95 | 944 | 96.6% | 1 |
| **0.99** | 910 | **97.8%** | **0** |

Recognition *rises* as the gate tightens, because the unidentifiable crops leave the denominator. It is **not** an occlusion detector: the ONNX model is the landmark regressor alone, with no detector head, so it is handed an ROI SCRFD has already called a face and returns points for whatever is inside — a face behind a paper tissue scores 0.9978.

**Reach.** At 640×480 with a ~65° horizontal FOV, 0.10 corresponds to a 48 px face box at roughly 2.2 m. Note the gate is scale-invariant: raising `HAL_CAMERA_WIDTH`/`HEIGHT` does not change which faces pass, but it does raise the pixel quality of the crop handed to the recognizer (EdgeFace warps to 112×112, and SCRFD returns bboxes in original-frame coordinates, so the crop comes from the full-resolution frame). At 1280×720 the same 0.10 yields a 72 px crop instead of 48 px.

**Three banks, three bars.** A face is a FRIEND when the enrolled **uploads**
score above `FACE_MATCH_THRESHOLD` (0.40), **or** the auto-captured **extended**
views score above `FACE_EXTENDED_THRESHOLD` (0.45). Otherwise it is a known
stranger when the **stranger bank** scores above `FACE_STRANGER_THRESHOLD`
(0.45). The identity comes from whichever bank *authorised* the match — an
extended view below its own threshold supplies neither the decision nor the name.

| bank | what it holds | domain | bar |
|------|------|------|------|
| uploads | the user's enrollment photo(s) | phone photo ↔ camera | `FACE_MATCH_THRESHOLD` 0.40 |
| `.extended` | views the device auto-captured of a recognised user | camera ↔ camera | `FACE_EXTENDED_THRESHOLD` 0.45 |
| strangers | one view per minted `stranger_N` | camera ↔ camera | `FACE_STRANGER_THRESHOLD` 0.45 |

The uploads are ground truth, but they are also the hardest match: a phone
photo against a ceiling camera. Measured on a lamp (2026-09-16) against the
enrollment photo, the owner scores **0.60–0.85** frontal, **0.29–0.34** at a ¾
pose, and **−0.31–0.28** in full profile — the same range as strangers and as a
photo held up on a phone. 0.40 sits in the gap between the ¾ and frontal
clusters. The previous 0.30 sat inside the ¾ cluster, where a genuine off-angle
frame and a similar-looking stranger overlap; a stranger accepted as the owner
is the worse failure. The cost is the ¾ band — the #299 replay puts the upload
path at 92.0% recall at 0.30 and 86.4% at 0.40 — and that band is exactly what
the extended bank exists to recover.

The two auto-captured banks are the same kind of evidence — a single view the
device captured on its own, camera-to-camera, never re-validated — and need the
same higher bar. Same-camera re-sightings of the same person land around 0.6.

A single shared threshold has no safe value. Measured over 990 logged frames
(2026-09-04): the enrollment photo alone keeps the best of six verified
strangers at 0.201, but **any** extended bank lifts that to 0.32–0.40, because
every stored view is another chance for a stranger to match something.

| `FACE_EXTENDED_THRESHOLD` | recognised | margin over the worst stranger (0.404) |
|------|------|------|
| 0.40 | 98.2% | **−0.004 — accepts that stranger** |
| **0.45** | **97.8%** | **+0.046** |
| 0.50 | 97.5% | +0.096 |
| 0.60 | 96.8% | +0.196 |

The four frames 0.40 would additionally recognise all have a confident
recognition 2–6 s away, so they cost nothing visible; a false acceptance does.

**Why the stranger bank is not matched at the upload bar (#429).** It was, and
on a device whose bank held 20 single-view rows minted before the quality gates
existed, one visitor was reported as `stranger_9` → `stranger_10` → `stranger_4`
within minutes: her frames scored **0.346 / 0.385 / 0.318** against three
different stale rows of three different people — each just clearing 0.30, the
argmax flipping with head pose — while her own two frames scored **0.658**
against each other. At 0.45 none of the stale rows match, she is minted after
`FACE_STRANGER_MIN_TICKS`, and her next frames match the fresh row at ~0.66.

**What gets auto-captured.** A recognised frame may join a user's extended bank
only when **both** hold: the enrolled uploads carried the match and scored above
`FACE_EXTEND_MIN_ENROLL_SIM` (0.45), and the view is different enough from what
is already stored to be worth keeping.

Both are needed. Novelty alone was the only test before, and "far from
everything we have" is equally the signature of a new pose and of a *different
person* — so the rule selected for what it should have screened out. The
farthest-point pruning that trims the bank is anchored on the uploads, so it
then ranks strangers highest and evicts genuine views to keep them. On one lamp
the bank had become 6/10 other people, matched at 1.000; fed nothing but genuine
frames of the enrolled user, the old rule still built a bank that accepted a
verified stranger at 0.362.

Requiring the uploads to carry the match also stops second-generation copies: a
frame recognised **by** the extended bank can no longer add to it, so one bad
view cannot breed more.

This does not make the bank pointless — the stored view is not there for its own
sake. A view the uploads recognise at 0.45–0.69 becomes a **new anchor** one step
further out in pose space. Replaying 990 logged frames: 879 frames were
recognised by the uploads directly, and **11 more were rescued by the extended
bank alone**, at enroll similarity 0.157–0.298 — below the upload bar (0.30 at the time, 0.40 now), so without
the bank they would have been missed. One of them is a frame the live device
labelled `stranger_4`.

**What is stored per view.** Each auto-captured view lives in
`<USERS_DIR>/<user>/.extended/` as three files sharing one stem:

| file | role |
|------|------|
| `ext_<ms>_<seq>.jpg` | the face crop |
| `ext_<ms>_<seq>.npy` | its embedding — what a restart reloads, so a hard pose never has to be re-detected |
| `ext_<ms>_<seq>.json` | **provenance**: why this view was admitted |

The provenance record holds the scores that admitted the view
(`enroll_similarity`, `extended_similarity`, `match_source`,
`max_sim_to_existing`), the frame's quality figures (`det_score`,
`landmark_score`, `face_height_ratio`, `truncation`, `bbox`), and the
`thresholds` in force at the time. That last field is the point: it lets you ask
"which views did the *old* rule let in" after a threshold change, without
guessing what the config was.

It is documentation, not state. The loader enumerates only `*.jpg`, so a missing
or malformed `.json` costs nothing at runtime, and writing it is best-effort —
the view stays valid if the write fails. Eviction deletes all three files
together. Views captured before this existed have no `.json`, which is itself a
useful signal: they predate the current rules.

Without it, a bank that turns out to hold the wrong views can only be wiped
wholesale — which is what had to happen on one lamp when 6 of 10 views turned out
to be other people.

**Minting an identity needs corroboration.** An unrecognised face does not get a `stranger_N` from a single frame. It must be seen `FACE_STRANGER_MIN_TICKS` times (2) within `FACE_STRANGER_CORROBORATION_S` (6 s), matched **by embedding** rather than by position, so "again" means the same person and not merely another face in the same corner.

Minting is the expensive verdict — a persistent identity, a stranger presence event, a row in the Unknown Faces card — and it is reached by scoring *below* both owner banks' negative bar (0.20) **and** below `FACE_STRANGER_THRESHOLD` — the stranger bank is not part of the negative test, because once it holds a dozen rows some row is nearly always above 0.20 and a genuinely new person could never mint (#429). Scoring there is what an unknown person looks like and equally what a momentarily unusable frame looks like. The gates above drop the unusable frames they can measure; this catches the rest by asking what no single frame can answer: is this face still there a tick later? Measured over 990 logged frames, 19 of the 28 runs that reach this branch are a single isolated tick.

The known cost of that gate: a returning stranger seen at a pose their single
stored view does not cover (0.20–0.45 against their own row) gets a second
`stranger_N`. One row cannot vouch for a pose it has never seen; a second id is
the honest outcome, and it beats being absorbed into somebody else's.

A real visitor is unaffected beyond one tick of delay: they are still there 2 s later and mint then. The window is deliberately ~3 sensing ticks rather than strictly back-to-back, so one dropped or blurred frame in the middle does not reset a genuine visitor's count.

The stranger gaze check reuses gaze wake's `GAZE_MAX_YAW_DEG`, `GAZE_EDGE_CONE_SCALE` and `GAZE_MIN_FACE_PX`, so tuning those for gaze wake also changes when strangers are greeted. It measures face-ID boxes, not gaze's face picker, so the picker's 15% floor (`HAL_GAZE_BEARING_MIN_FACE_HEIGHT_FRAC`, #567) does not apply to it.

Each face-ID tick with an ungreeted stranger in frame logs one line, with the numbers behind each stranger's vote and their running count:

```
[face] stranger gaze: stranger_16 yaw=51.6<=92.6 face=117px>=48 edge=0.68 -> facing 2/2; stranger_17 yaw=70.2>62.4 face=40px<48 edge=0.05 -> away (face too small, turned too far) 0/1; stranger_18 yaw=- face=120px>=48 edge=0.10 -> away (landmarks off-frame) 0/3
```

`yaw` is the head yaw in degrees, shown against the cone allowed at that edge (`GAZE_MAX_YAW_DEG` widened by `GAZE_EDGE_CONE_SCALE`): `<=` passes, `>` fails. `face` is the face height in the gaze watcher's 640-wide pixels, shown against `GAZE_MIN_FACE_PX`: `>=` passes, `<` fails. A tick votes `facing` only when both pass. `edge` is the distance from frame centre (0 = centre, 1 = edge). An `away` vote names what failed in brackets: `face too small`, `turned too far`, or, when yaw could not be measured at all (`yaw=-`), `no keypoints`, `landmarks off-frame`, `no yaw` or `no frame`. An unmeasurable face never counts as facing, but it isn't a stranger who turned away either.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Distant people not recognized | Decrease `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.07) |
| Someone else is recognized as an enrolled user | Raise `FACE_EXTENDED_THRESHOLD` (0.45 → 0.50) and check `match_source` in the face debug log — `extended` means an auto-captured view carried it |
| The enrolled user is `unsure` at a ¾ head turn | Expected while the extended bank is refilling — those poses score 0.29–0.34 against a frontal upload. Check `.extended/` is growing; only lower `FACE_MATCH_THRESHOLD` (0.40 → 0.35) after re-measuring against known strangers |
| One visitor is reported under several `stranger_N` ids within minutes | Stale stranger rows are being false-accepted (#429). Check `stranger_similarity` in the face debug log sits just over the bar; raise `FACE_STRANGER_THRESHOLD` (0.45 → 0.50), and clear `/root/local/strangers/*.npy` if the rows predate 2026-09-04 (no quality gates) |
| A returning visitor keeps getting a new `stranger_N` | Their single stored view does not cover the pose. Lower `FACE_STRANGER_THRESHOLD` (0.45 → 0.40) only with headroom over the false accepts in the debug log; below 0.40 #429 comes back |
| An enrolled user is missed at angles the frontal photo cannot cover | Lower `FACE_EXTENDED_THRESHOLD`, but not below 0.45 without re-measuring against known strangers |
| Extended bank fills with other people | Should no longer happen; if it does, raise `FACE_EXTEND_MIN_ENROLL_SIM` and check each stored view's `match_source` in the face debug log |
| Extended bank stays empty or stops growing | Lower `FACE_EXTEND_MIN_ENROLL_SIM` (0.45 → 0.40). Only frames the *uploads* recognise are eligible, so a user with a single frontal photo grows the bank slowly by design |
| False detections from tiny face-like patches | Increase `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.15) |
| Recognition flickers / mints new `stranger_N` ids repeatedly | Crop is too small to embed reliably — increase `FACE_HEIGHT_RATIO_THRESHOLD`, or raise camera resolution to 1280×720 |
| Wrong person matched when someone sits close to a frame edge | Face is clipped — decrease `FACE_MAX_TRUNCATION` (0.05 → 0.03), or re-aim the camera so heads stay fully in frame |
| People at the frame edge stop being recognized at all | Increase `FACE_MAX_TRUNCATION` (0.05 → 0.10); check for `FAIL-truncated` folders in the face debug log to see how much was actually cut |
| Lamp mints a `stranger_N` for the enrolled user while it is panning | Motion blur — that is what `FACE_MIN_SHARPNESS` screens out; check `FAIL-blurred` folders for the sharpness actually seen |
| Unknown visitors take too long to be noticed | Lower `FACE_STRANGER_MIN_TICKS` to 1 to mint from a single frame (the old behaviour) |
| Spurious `stranger_N` identities still appear | Raise `FACE_STRANGER_MIN_TICKS` to 3; each step costs a visitor one more sensing tick |
| Lamp never greets a visitor who looked at it | Lower `FACE_STRANGER_GAZE_MIN_FACING` (2 → 1); check `[face] stranger gaze:` in the HAL log |
| Visitor who looked briefly is not greeted because ticks are slow or skipped | Raise `FACE_STRANGER_GAZE_WINDOW_S` (10 → 15); too large lets glances minutes apart add up |
| Recognition drops out in a dim room after an update | Laplacian variance falls with light; lower `FACE_MIN_SHARPNESS` (100 → 70) and re-check `FAIL-blurred` |
| Lamp mints `stranger_N` ids for the enrolled user at close range | The detector is firing on an ear or similar — that is what `HAL_FACE_LANDMARK_CONF_THRESHOLD` 0.99 screens out |
| Faces that are plainly fine stop being recognized after an update | Lower `HAL_FACE_LANDMARK_CONF_THRESHOLD` (0.99 → 0.95); the default is tuned on one device |
| Presence events fire too often | Increase `FACE_COOLDOWN_S` (10 → 30) |
| Lamp forgets owner too quickly after leaving | Increase `FACE_OWNER_FORGET_S` |

---

## Per-Face Motion Detection

**File:** `hal/config.py`

```python
MOTION_PER_FACE_ENABLED = false            # Enable per-face action recognition
MOTION_PER_FACE_DEDUP_WINDOW_S = 300.0     # Per-action dedup window (5 min)
MOTION_PER_FACE_SESSION_TTL_S = 30.0       # Evict face session after this long unseen
MOTION_PER_FACE_MIN_FRAMES = 4             # Min frames before first event fires
```

Per-face motion opens a separate WS session per detected face and runs action recognition on an expanded face crop. Each action is deduped independently per face. On top of the per-face dedup, ONE global cooldown floor is shared across all faces — same semantics and same knobs as regular motion (`MOTION_EVENT_COOLDOWN_S` same-class floor, `MOTION_TRANSITION_MIN_GAP_S` min gap on the class-transition bypass, floor cleared on a real user change) — so N faces in frame still produce at most one same-class `motion.activity` per cooldown, not N.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Too many events per person | Increase `MOTION_PER_FACE_DEDUP_WINDOW_S` (300 → 600) |
| Too many events across people | Increase `MOTION_EVENT_COOLDOWN_S` — the global floor is shared with regular motion |
| Noisy single-frame classifications | Increase `MOTION_PER_FACE_MIN_FRAMES` (4 → 8) |
| Sessions accumulate for briefly-seen faces | Decrease `MOTION_PER_FACE_SESSION_TTL_S` (30 → 15) |
| WS connections pile up in multi-person scenes | Disable with `MOTION_PER_FACE_ENABLED=false` |

---

## Speech Emotion Recognition (SER)

**Files:** `hal/config.py`, `hal/drivers/voice/voice_service.py` (`_submit_speech_emotion_from_session`, `_identify_and_decorate`, `_session_wav_for_ser`) — see also [Speech Emotion Recognition](../../../docs/speech-emotion.md) for the full architecture. **Vietnamese:** [docs/vi/sensing-tuning_vi.md](vi/sensing-tuning_vi.md) (SER section), [speech-emotion_vi.md](../../../docs/vi/speech-emotion_vi.md).

**Voice integration (session end, transcript-independent):** in the `finally` block of every mic session (VAD trigger → ~2.5 s silence), `_stream_session` runs `_identify_and_decorate(final_text, audio_buffer)` **once** to resolve both `final_msg` (for the Lamp POST when STT had text) and `user_name` (for the SER submit). The result is passed to `_submit_speech_emotion_from_session(audio_buffer, user=...)`, which builds the WAV and calls `SpeechEmotionService.submit`. Unknown / no-match speakers still enqueue SER under the shared `unknown` dedup key when audio is long enough.

```python
SPEECH_EMOTION_ENABLED = True
SPEECH_EMOTION_FLUSH_S = 10.0               # Per-user buffer drain cadence
SPEECH_EMOTION_DEDUP_WINDOW_S = 300.0       # (user, bucket) TTL — 5 min
SPEECH_EMOTION_MIN_AUDIO_S = 3.0            # Skip utterances shorter than this (hal.config default)
SPEECH_EMOTION_API_TIMEOUT_S = 15           # perception-service HTTP timeout
DL_SER_ENDPOINT = "/lelamp/api/dl/ser/recognize"
```

Per-label confidence thresholds are **not** in `config.py` — they live in `hal/drivers/voice/speech_emotion/constants.py` as `CONFIDENCE_THRESHOLD_BY_LABEL` (and `DEFAULT_CONFIDENCE_THRESHOLD` for unmapped labels). Negative emotions are gated higher than positive ones to suppress false-positive alarms:

```python
# constants.py
CONFIDENCE_THRESHOLD_BY_LABEL = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
```

Tune by editing the dict directly — no env override.

**How to read the log:**

The service tags every line `[speech_emotion]`:

```
INFO lelamp.voice.speech_emotion: [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO lelamp.voice.speech_emotion: [speech_emotion] flushing alice: Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...) (mode of sad, fearful, sad)
INFO lelamp.voice.speech_emotion: [speech_emotion] sent to Lamp: Speech emotion detected: Sad. ...
INFO lelamp.voice.speech_emotion: [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

The `flushing` line shows the raw label list — that's the mode-over-samples that produced the dominant label.

**Tuning:**

| Symptom | Fix |
|---------|-----|
| Same-bucket events fire too often | Increase `SPEECH_EMOTION_DEDUP_WINDOW_S` (300 → 600) |
| Single-utterance noisy reads slip through | Raise the offending label's entry in `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) — e.g. nudge `"sad": 0.6 → 0.7`. Bump `DEFAULT_CONFIDENCE_THRESHOLD` only if the noise is across the board |
| Short "yeah" / "ok" utterances flagged | Increase `SPEECH_EMOTION_MIN_AUDIO_S` (3.0 → 4.0) |
| Mood lag — Lamp too slow to react after a real shift | Decrease `SPEECH_EMOTION_FLUSH_S` (10 → 5) |
| Worker queue full warnings in log | Investigate perception-service latency; raising queue size is not enough — backlog means something downstream is wedged |
| Too many `speech_emotion.detected` for strangers | Expected: unknown speakers use `user="unknown"`; tighten the per-label entry in `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) or dedup window — do not disable SER solely because Lamp transcript says `Unknown Speaker:` |

---

## Apply Changes

After editing `hal/config.py` or `voice_service.py` on the Pi:

```bash
sudo systemctl restart lelamp
tail -f /var/log/hal/server.log
```

No reboot needed — just restart the service.

### Post-capture voice noise guard (lamp-52e6 trial)

The measured trial uses `HAL_SILERO_THRESHOLD=0.15`, `HAL_REALTIME_NOISE_SPEECH_RATIO=0.45`, and `HAL_VOICE_NOISE_MIN_VOICED_MS=160` in `/opt/hal/.env`, followed by a HAL restart. The duration condition requires code supporting the new key; lowering the ratio alone does not reject isolated high-ratio noise spikes. These values are stored in `robots/lamp/rootfs/opt/hal/.env` as the lamp profile defaults. The shared code defaults remain ratio `0.55` and minimum duration `0`; microphone variants still need their own acoustic validation.

On 2026-10-08, 12 captures through the device's existing post-AEC pipeline covered background (6), servo (3), and speaker echo (3). Two previously recorded human utterances plus 14 derived gain/padding variants were replayed on-device. The old ratio-only guard accepted 7/12 noise samples and 15/16 speech samples; the trial accepted 0/12 and 16/16 respectively. These are guard decisions, not end-to-end false dispatch rates. The variants are not independent human recordings, and tuning and evaluation used the same small corpus; broader speech, physical tap, and sleep/wake validation remain necessary.

The guard also applies in automatic mode and before OS fallback when realtime is off/unavailable. Only empty/short transcripts selected by the existing policy are checked; other transcript filters remain unchanged. Logs expose `voiced_ms`, `min_ratio`, `min_voiced_ms`, and `accepted`. The duration is cumulative voiced audio, not a required delay: the extra decision is arithmetic on the existing Silero result.
