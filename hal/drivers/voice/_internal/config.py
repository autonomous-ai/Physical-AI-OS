"""Voice service environment-variable configuration.

All HAL_* knobs live here so voice_service.py doesn't need a 60-line
config preamble. Defaults match the previous in-line values.
"""

import os
from pathlib import Path

from hal import config as _hal_config


# ---------------------------------------------------------------------------
# OS server endpoint
# ---------------------------------------------------------------------------
OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"
OS_HARNESS_FOLLOWUP_URL = "http://127.0.0.1:5000/api/harness/voice-followup"
# Dead-air filler for the realtime wait. os-server owns the phrase pools, the
# language resolution, and the WAV cache; HAL only decides WHEN the wait has run
# long enough to deserve one. See PlayFiller in
# system/server/sensing/delivery/http/deadair_filler.go.
OS_FILLER_URL = "http://127.0.0.1:5000/api/sensing/filler"


# ---------------------------------------------------------------------------
# Audio framing — must stay device-rate-independent
# ---------------------------------------------------------------------------
STT_RATE = 16000             # Rate expected by all STT providers
CHANNELS = 1
FRAME_DURATION_MS = 64       # Frame duration in ms


# ---------------------------------------------------------------------------
# Local VAD — RMS energy gate
# ---------------------------------------------------------------------------
RMS_THRESHOLD = int(os.environ.get("HAL_VAD_THRESHOLD", "3500"))
SILENCE_TIMEOUT_S = float(os.environ.get("HAL_SILENCE_TIMEOUT", "2.5"))
SPEECH_HOLDOFF_S = float(os.environ.get("HAL_SPEECH_HOLDOFF", "0.2"))
# Pre-roll lookback — 8 × 64ms = 512ms of audio history before VAD trigger so
# quiet first syllables ("b", "k", "t", "p") reach STT instead of getting clipped.
PRE_ROLL_FRAMES = int(os.environ.get("HAL_PRE_ROLL_FRAMES", "8"))
SESSION_COOLDOWN_S = float(os.environ.get("HAL_SESSION_COOLDOWN_S", "0.3"))


# ---------------------------------------------------------------------------
# Silero VAD (semantic, ONNX) — rejects TV/music/non-speech audio
# ---------------------------------------------------------------------------
SILERO_VAD_ENABLED = os.environ.get("HAL_SILERO_ENABLED", "false").lower() == "true"
SILERO_VAD_THRESHOLD = float(os.environ.get("HAL_SILERO_THRESHOLD", "0.3"))
SILERO_CHUNK_SIZE = int(os.environ.get("HAL_SILERO_CHUNK_SIZE", "512"))
SILERO_MODEL_PATH = Path(__file__).resolve().parent.parent / "resources" / "silero_vad.onnx"

# Silero on the SILENCE clock (end of turn), not just the entry gate.
#
# Closing a session is driven by RMS alone: any frame above RMS_THRESHOLD
# refreshes the silence timer. In a noisy room the noise floor sits above the
# threshold, so the timer never expires — the turn runs to MAX_SESSION_DURATION
# and ships mostly room noise to STT, which comes back empty or as junk and the
# device answers nothing (device-observed 18/08/2026: sessions of 8-25s with
# transcript='(empty)'). Energy VAD misses roughly half of real speech frames
# in noise; production voice stacks (Pipecat, LiveKit, Deepgram) all put a
# neural VAD on this decision instead.
#
# So RMS stays as the cheap first gate, and Silero confirms before the timer is
# actually refreshed. Batched over a window rather than run per frame: Silero
# costs ~20ms/frame on ARM and its LSTM wants more than one 64ms frame to
# settle. Set HAL_SILENCE_VAD_ENABLED=false to fall back to pure RMS.
SILENCE_VAD_ENABLED = os.environ.get("HAL_SILENCE_VAD_ENABLED", "true").lower() == "true"
SILENCE_VAD_WINDOW_FRAMES = int(os.environ.get("HAL_SILENCE_VAD_WINDOW_FRAMES", "3"))


# ---------------------------------------------------------------------------
# WebRTC VAD — fast C-based pre-filter (~0.1ms vs Silero ~20ms)
# ---------------------------------------------------------------------------
WEBRTCVAD_ENABLED = os.environ.get("HAL_WEBRTCVAD_ENABLED", "false").lower() == "true"
WEBRTCVAD_AGGRESSIVENESS = int(os.environ.get("HAL_WEBRTCVAD_AGGRESSIVENESS", "2"))
WEBRTCVAD_FRAME_MS = int(os.environ.get("HAL_WEBRTCVAD_FRAME_MS", "30"))


# ---------------------------------------------------------------------------
# Echo handling — adaptive RMS gate after TTS + transcript similarity filter
# ---------------------------------------------------------------------------
ECHO_RMS_FLOOR = int(os.environ.get("HAL_ECHO_RMS_FLOOR", "200"))
ECHO_GATE_MAX_WAIT_S = float(os.environ.get("HAL_ECHO_GATE_MAX_WAIT_S", "1.5"))
ECHO_GATE_WINDOW_S = float(os.environ.get("HAL_ECHO_GATE_WINDOW_S", "0.05"))
ECHO_SIMILARITY_THRESHOLD = float(os.environ.get("HAL_ECHO_SIMILARITY_THRESHOLD", "0.55"))
ECHO_RELEVANCE_WINDOW_S = float(os.environ.get("HAL_ECHO_RELEVANCE_WINDOW_S", "15.0"))
MAX_SESSION_DURATION_S = float(os.environ.get("HAL_MAX_SESSION_DURATION_S", "30"))
# Hands-free capture with recognized words can outlive the short noise/manual
# capture ceiling. A hard limit aborts; it is never permission to execute a
# possibly unfinished request.
TURN_END_ENABLED = os.environ.get("HAL_TURN_END_ENABLED", "true").lower() == "true"
TURN_END_FALLBACK_S = float(os.environ.get("HAL_TURN_END_FALLBACK_S", "2.5"))
TURN_END_MAX_PAUSE_S = float(os.environ.get("HAL_TURN_END_MAX_PAUSE_S", "6.0"))
TURN_END_MAX_DURATION_S = float(os.environ.get("HAL_TURN_END_MAX_DURATION_S", "180"))

# Warm mic — keep the arecord capture stream OPEN across TTS/music (drain +
# discard frames) instead of closing it and paying a cold arecord reopen
# (~1s on slow USB mics) on the next turn. That reopen latency is dead air
# right after a push-to-talk cue ("listening!"), so the user's first words
# land before the mic is live and get clipped. Default off → legacy behavior
# (close on TTS, reopen after). Opt in with HAL_WARM_MIC=true.
WARM_MIC = os.environ.get("HAL_WARM_MIC", "true").lower() == "true"
# Max echo-skip after TTS/music ends before resuming VAD (warm mic only).
# Bounded ≪ the legacy 1.5s reverb gate so a user who talks right after a cue
# resumes fast and the pre-roll lookback captures their opening words. Skips
# early once the room drops below ECHO_RMS_FLOOR.
WARM_MIC_ECHO_SKIP_MAX_S = float(os.environ.get("HAL_WARM_MIC_ECHO_SKIP_MAX_S", "0.1"))


# ---------------------------------------------------------------------------
# Acoustic echo cancellation (WebRTC AEC3) — see drivers/voice/aec.py.
# On by default. It needs the `aec-audio-processing` native binding, which is
# not a hal dependency — absent, every AEC entry point degrades to a no-op and
# the voice path behaves exactly as it did before, so defaulting this on cannot
# break a device that lacks the binding.
# Hardware-AEC profiles disable this explicitly. With LIVE_MODE enabled,
# that selects the shared adaptive live path; no hardware name is inspected.
# ---------------------------------------------------------------------------
AEC_ENABLED = os.environ.get("HAL_AEC_ENABLED", "true").lower() == "true"
# Speaker→mic delay hint. AEC3 estimates the real delay itself, but the hint
# decides how fast it converges. Measured from HAL's own aec_mic/aec_ref dump
# (24/08/2026, lamp-ee17): the lag between the frames the APM is actually
# handed is 204ms median, not the 150 this used to declare. It also drifts
# 154→215ms over 93s (~667ppm) because the mic and speaker are separate USB
# devices with independent clocks — which is why this is a per-device value and
# not a constant. Re-measure with HAL_AEC_DUMP_DIR after any audio hardware
# change; a wrong hint costs convergence, not correctness.
AEC_DELAY_MS = int(os.environ.get("HAL_AEC_DELAY_MS", "205"))
AEC_NOISE_SUPPRESSION = os.environ.get("HAL_AEC_NS", "true").lower() == "true"
# Keep cancelling for this long after the last speaker write, then bypass the
# APM until playback resumes. 2.0, not 0.5: the gaps between the sentences of
# one reply are longer than half a second, so a 0.5s tail let the canceller
# bypass and reset mid-reply and then re-converge from cold. Every bypass costs
# convergence — the ERLE logged in the window right after `AEC engaged` ranges
# from -25.1 dB to 23.2 dB, against 15-23 dB once settled.
AEC_TAIL_S = float(os.environ.get("HAL_AEC_TAIL_S", "2.0"))
# Depth of the echo-reference FIFO. It must hold everything written but
# not yet heard: the tap fires when ALSA ACCEPTS audio, the mic hears it a
# full output buffer later, and TTS writes in network-paced bursts. When
# the FIFO is shallower than that lead, the oldest bytes -- exactly the
# ones the mic is about to hear -- are dropped and the reference then runs
# dry for the rest of the burst. Measured on lamp-ee17 at 500ms: the
# reference underran on 30-98% of processed frames during a reply.
AEC_REF_MS = int(os.environ.get("HAL_AEC_REF_MS", "500"))
# Set to a directory to write aec_mic/ref/out.wav for offline ERLE analysis.
AEC_DUMP_DIR = os.environ.get("HAL_AEC_DUMP_DIR", "")


# ---------------------------------------------------------------------------
# STT keepalive — pre-connect WS before speech is detected to cut latency
# ---------------------------------------------------------------------------
STT_KEEPALIVE = os.environ.get("HAL_STT_KEEPALIVE", "false").lower() == "true"
# Send a KeepAlive every N seconds while pre-connected and idle, so the server
# doesn't idle-close the WS (~10s) and force a slow cold-reconnect at speech start
# (the cause of empty transcripts on short/quiet utterances). Must be < server
# idle timeout.
STT_KEEPALIVE_PING_S = float(os.environ.get("HAL_STT_KEEPALIVE_PING_S", "3"))

# ---------------------------------------------------------------------------
# Speaker-ID prepass — bounded waits before commit and downstream dispatch
# ---------------------------------------------------------------------------
# Recognition runs in the background. Turn-based realtime gives it only the
# short COMMIT budget before sending activityEnd; the normal JOIN budget is
# retained before downstream dispatch and for live/non-realtime paths.
SPEAKER_PREPASS_JOIN_S = float(os.environ.get("HAL_SPEAKER_PREPASS_JOIN_S", "2.0"))
# Brief pre-commit opportunity; the remaining identity work overlaps the reply.
SPEAKER_PREPASS_COMMIT_JOIN_S = float(os.environ.get("HAL_SPEAKER_PREPASS_COMMIT_JOIN_S", "0.2"))

# How long a resolved speaker identity is reused instead of re-running the
# recognizer. The prepass is an external inference call on every turn — a
# conversation of ten turns paid for ten of them to be told the same name, while
# each call adds ~1.4s in front of the model hearing the audio (lamp-0c89,
# 03/09/2026). Voices do not change mid-conversation; the cache is what a face
# identity already gets by aging out rather than being re-derived per frame.
#
# UNKNOWN is cached too, and deliberately: an utterance the recognizer could not
# place is the case most likely to repeat (a guest, a bad angle, a short clip),
# and retrying it every turn pays the full latency for the same non-answer.
SPEAKER_ID_CACHE_S = float(os.environ.get("HAL_SPEAKER_ID_CACHE_S", "90"))
# Inside a wake-word follow-up window the turns are one conversation by
# definition, so the cache holds for the whole window regardless of the TTL
# above. 0 disables the extension.
SPEAKER_ID_CACHE_FOLLOWUP_S = float(
    os.environ.get("HAL_SPEAKER_ID_CACHE_FOLLOWUP_S", "300")
)


# ---------------------------------------------------------------------------
# Speaker recognition — prefix every transcript with "<Name>: "
# ---------------------------------------------------------------------------
SPEAKER_RECOGNITION_ENABLED = _hal_config.SPEAKER_RECOGNITION_ENABLED
SPEAKER_MIN_AUDIO_S = _hal_config.SPEAKER_MIN_AUDIO_S
SPEECH_EMOTION_ENABLED = _hal_config.SPEECH_EMOTION_ENABLED


# ---------------------------------------------------------------------------
# Wake words — fallback derived from the device type (lamp/dog/intern) when no
# IDENTITY.md name is set, so an unnamed device isn't hardcoded to "lamp".
# Last resort "friend" when device_type is also unavailable.
# ---------------------------------------------------------------------------
_wake_name = _hal_config.resolve_device_type("friend")
WAKE_WORD_PREFIXES = ("hello", "hey", "hi", "alo", "okay", "ok", "wake up")
DEFAULT_WAKE_WORDS = [
    *(f"{prefix} autonomous" for prefix in WAKE_WORD_PREFIXES),
    *(f"{prefix} {_wake_name}" for prefix in WAKE_WORD_PREFIXES),
]


# ---------------------------------------------------------------------------
# Enroll-nudge cooldown
# ---------------------------------------------------------------------------
ENROLL_NUDGE_COOLDOWN_S = float(os.environ.get("HAL_ENROLL_NUDGE_COOLDOWN_S", str(30 * 60)))


# ---------------------------------------------------------------------------
# End-of-turn latency — close capture on the STT endpoint, not on the long clock
# ---------------------------------------------------------------------------
# SILENCE_TIMEOUT_S above is the fallback clock: it has to be long because an
# empty/noisy session has no other evidence that the user is done. But once STT
# has delivered a FINAL segment, the provider has already made that call for us
# (Flux emits EndOfTurn; nova fires is_final after its own endpointing window),
# so waiting the full 2.5s afterwards is dead air in front of every single
# realtime turn — the largest fixed cost between the user falling silent and the
# model hearing the commit.
#
# So: with a final in hand, close after this much local silence instead. Kept as
# a separate (shorter) clock rather than lowering SILENCE_TIMEOUT_S, because a
# turn with no final still needs the long one. Raise it if the device starts
# cutting people off at natural mid-sentence pauses; 0 disables (back to the
# single long clock).
ENDPOINT_SILENCE_S = float(os.environ.get("HAL_ENDPOINT_SILENCE_S", "0.8"))


# ---------------------------------------------------------------------------
# Live (full-duplex) mode — see hal.config.LIVE_MODE and
# voice_service._live_session. Enabled process-wide by HAL_LIVE_MODE=true; the
# knobs below only shape a session once one is open.
# ---------------------------------------------------------------------------
LIVE_MODE = _hal_config.LIVE_MODE

# Linear PCM gain while hardware-AEC live playback is temporarily ducked.
# Keep the demo default until a device-specific value has been measured.
def _live_duck_gain(value):
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return 0.12
    return gain if 0.0 < gain <= 1.0 else 0.12


LIVE_DUCK_GAIN = _live_duck_gain(os.environ.get("HAL_LIVE_DUCK_GAIN", "0.12"))

# What goes on the uplink while our own speaker is playing.
#
#   "mute"      — substitute silence for the whole playback window. Ships today.
#                 Costs barge-in entirely: the user cannot interrupt until the
#                 device stops talking, because the provider's VAD receives
#                 digital silence for the whole reply and has nothing to detect.
#   "cancelled" — send the cancelled frame, and substitute silence only for
#                 frames the canceller could not vouch for. TRUE full duplex —
#                 but only as good as the reference: every frame where the
#                 reference FIFO underran is still substituted, and on a device
#                 where that is most of them the provider still hears mostly
#                 silence. Measured on lamp-ee17 2026-09-08: 50-99% of playback
#                 frames underran ("reference UNDERRAN on 170 frames" against
#                 27 cancelled), so `cancelled` alone does NOT restore barge-in
#                 there. Check the ERLE log before assuming it will.
#   "always"    — never substitute during playback; hand the provider every
#                 frame and let ITS VAD separate the user from our own echo.
#                 The only mode in which provider-native barge-in can work on a
#                 device with a starving reference, and the least safe: with
#                 low ERLE the model hears its own onsets as the user and cuts
#                 itself off (see LIVE_MAX_UNPROMPTED_REPLIES for the loop).
#                 Lower the speaker volume before using it.
#
# "cancelled" is the intended end state and is NOT the default, because the
# canceller does not yet earn it. Measured 2026-09-04 (barge-in-captures/):
# mean ERLE 14-19 dB but PEAK ERLE ~5 dB — mic peaks of 29264 leave the APM at
# 25269, against a real-interruption floor of 6956. The provider's VAD sees
# peaks and has no echo defence of its own (it cannot see uncancelled(), and
# it applies no duration floor), so it reads the device's own onsets as the
# user interrupting and the reply cuts itself off.
# Flip this to "cancelled" once a replay of full40-bargein-off through the
# canceller puts peak residual under that floor with margin.
LIVE_UPLINK_DURING_PLAYBACK = os.environ.get(
    "HAL_LIVE_UPLINK_DURING_PLAYBACK", "mute"
).strip().lower()

# How long after the last reference write or observed TTS end the room still
# counts as "playing", including when AEC is unavailable.
# The ACOUSTIC tail, deliberately not AEC_TAIL_S (2.0s): keyed on the longer
# one, "mute" swallows the first two seconds of every reply the user gives.
LIVE_PLAYBACK_TAIL_S = float(os.environ.get("HAL_LIVE_PLAYBACK_TAIL_S", "0.35"))

# Hang up after this long with no action from the USER, then hand the mic back
# to the VAD, which opens a new session on the next real speech. A live session
# bills upstream audio for every second it is open, against only speech segments
# on the turn-based path, so a session nobody ended must end itself.
#
# The model's own reply does not count as user action, but the clock is held
# while the device is speaking so a long answer is never cut off mid-sentence:
# the window runs from whichever came later, the user's last words or the moment
# the device stopped talking.
LIVE_IDLE_HANGUP_S = float(os.environ.get("HAL_LIVE_IDLE_HANGUP_S", "15"))
# What counts as "the user said something" for that window. The local gate
# (RMS + Silero) confirms that SOMEONE spoke near the mic, which held a GPT-Live
# session open for two minutes of room chatter the model never transcribed
# (lamp-ee17, 2026-09-17). With this on, only speech the provider actually
# transcribed (a UserSpeechOutput carrying text) refreshes the clock; the local
# gate still resets the unprompted-reply counter. Off = the local gate alone.
LIVE_IDLE_REQUIRES_TRANSCRIPT = os.environ.get(
    "HAL_LIVE_IDLE_REQUIRES_TRANSCRIPT", "true"
).strip().lower() in ("1", "true", "yes")

# Hard ceiling on a model that has started answering ITSELF, counted in REPLIES rather than seconds.
LIVE_MAX_UNPROMPTED_REPLIES = int(
    os.environ.get("HAL_LIVE_MAX_UNPROMPTED_REPLIES", "3")
)

# Grace period after the model calls end_conversation, before the session is
# actually torn down. The model normally speaks its farewell in the SAME turn
# as the tool call, so hanging up on the call itself would cut it off mid-word.
# Long enough for a short sign-off, short enough not to feel like a hang.
LIVE_HANGUP_GRACE_S = float(os.environ.get("HAL_LIVE_HANGUP_GRACE_S", "3"))
LIVE_UPLINK_DUMP_DIR = os.environ.get("HAL_LIVE_UPLINK_DUMP_DIR", "")

# Absolute ceiling on one session, whatever is happening. Backstop against a
# session that never goes idle because the room is noisy.
LIVE_MAX_S = float(os.environ.get("HAL_LIVE_MAX_S", "600"))
