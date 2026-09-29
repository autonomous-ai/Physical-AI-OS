"""HAL runtime configuration; all values read from environment variables."""

import os
import tempfile
from pathlib import Path
from typing import Optional, Union

SERVO_PORT = os.environ.get("HAL_SERVO_PORT", "/dev/ttyACM0")
DEVICE_ID = os.environ.get("HAL_DEVICE_ID", "hal")
SERVO_FPS = int(os.environ.get("HAL_SERVO_FPS", "30"))
SERVO_HOLD_S = float(os.environ.get("HAL_SERVO_HOLD_S", "3.0"))
# Seconds to interpolate from the current pose to a recording's first frame.
SERVO_PLAY_RAMP_S = float(os.environ.get("HAL_SERVO_PLAY_RAMP_S", "2.0"))
HTTP_PORT = int(os.environ.get("HAL_HTTP_PORT", "5001"))
# production: bind 127.0.0.1 + local-only middleware; developer: bind 0.0.0.0, no restrictions.
_mode = os.environ.get("HAL_MODE", "production").strip().lower()
MODE: str = "developer" if _mode == "developer" else "production"
HTTP_HOST: str = "0.0.0.0" if MODE == "developer" else "127.0.0.1"
CAMERA_INDEX = int(os.environ.get("HAL_CAMERA_INDEX", "0"))
# Absolute path or v4l2 name substring (e.g. "OPENAICAM"); falls back to HAL_CAMERA_INDEX.
CAMERA_NAME = os.environ.get("HAL_CAMERA_NAME", "").strip() or None
CAMERA_WIDTH = int(os.environ.get("HAL_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("HAL_CAMERA_HEIGHT", "480"))
# AUTO by default: manual exposure with high gain corrupts ISP colors. Manual: keep gain <= ~144;
# exposure_absolute is V4L2 x100us (e.g. 330 = 33ms).
CAMERA_AUTO_EXPOSURE = os.environ.get("HAL_CAMERA_AUTO_EXPOSURE", "auto").strip().lower()
CAMERA_EXPOSURE = int(os.environ.get("HAL_CAMERA_EXPOSURE", "330"))
# >~144 risks ISP color corruption; also pins retained gain in auto mode.
CAMERA_GAIN = int(os.environ.get("HAL_CAMERA_GAIN", "96"))
# Camera-specific (e.g. -64..64); unset = camera default.
CAMERA_BRIGHTNESS = int(os.environ["HAL_CAMERA_BRIGHTNESS"]) if os.environ.get("HAL_CAMERA_BRIGHTNESS") else None

# Overrides bypass auto-detection, e.g. HAL_AUDIO_INPUT_ALSA=plughw:1,0
AUDIO_INPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_INPUT_ALSA") or None
AUDIO_OUTPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_OUTPUT_ALSA") or None
# HFP routes the headset mic (mono 16kHz); off = A2DP + built-in mic.
BT_PREFER_HFP: bool = os.environ.get("HAL_BT_PREFER_HFP", "0") == "1"
# Noise-sensing mic: sounddevice index or ALSA name (e.g. "plughw:6,0").
_sensing_device_env = os.environ.get("HAL_AUDIO_SENSING_DEVICE")
AUDIO_SENSING_DEVICE: Optional[Union[int, str]] = None
if _sensing_device_env:
    try:
        AUDIO_SENSING_DEVICE = int(_sensing_device_env)
    except ValueError:
        AUDIO_SENSING_DEVICE = _sensing_device_env
# 1.0 = normal, max 4.0.
TTS_SPEED: float = float(os.environ.get("HAL_TTS_SPEED", "1.2"))
TTS_VOICE: str = os.environ.get("TTS_VOICE", "nova")
TTS_INSTRUCTIONS: str = os.environ.get("HAL_TTS_INSTRUCTIONS", "Friendly")
# ElevenLabs WebSocket stream-input instead of HTTP chunked streaming.
TTS_ELEVENLABS_WS: bool = os.environ.get("HAL_TTS_ELEVENLABS_WS", "false").lower() in ("1", "true", "yes")

# Local YOLOv8n for COCO targets; false forces remote YOLOWorld (open vocabulary).
TRACKING_DETECT_LOCAL_ENABLED: bool = os.environ.get(
    "HAL_TRACKING_DETECT_LOCAL", "true"
).strip().lower() in ("1", "true", "yes", "on")

# Local YuNet for target='face' (else remote YOLOWorld, ~0.55s median).
TRACKING_FACE_DETECTOR_ENABLED: bool = os.environ.get(
    "HAL_TRACKING_FACE_DETECTOR", "true"
).strip().lower() in ("1", "true", "yes", "on")

TRACKING_MAX_DURATION_S: float = float(
    os.environ.get("HAL_TRACKING_MAX_DURATION_S", "10")
)


OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"
# Loopback: lets os-server retire a removed person's USER.md profile immediately.
OS_USER_RECONCILE_URL = "http://127.0.0.1:5000/api/agent/user-reconcile"
# Named-pool filler (os-server owns phrases + language + WAV cache).
OS_SENSING_FILLER_URL = "http://127.0.0.1:5000/api/sensing/filler"
# Requires os-server's `look.capture` handler; set false if HAL ships ahead of it.
LOOK_MONITOR_ENABLED: bool = (
    os.environ.get("HAL_LOOK_MONITOR", "true").lower() in ("1", "true", "yes")
)
OS_WELLBEING_LOG_URL = "http://127.0.0.1:5000/api/wellbeing/log"
GUARD_STATUS_URL = "http://127.0.0.1:5000/api/guard"
GUARD_CHECK_INTERVAL_S = float(os.environ.get("HAL_GUARD_CHECK_INTERVAL_S", "10.0"))

EVENT_COOLDOWN_S = float(os.environ.get("HAL_EVENT_COOLDOWN_S", "60.0"))

SOUND_RMS_THRESHOLD = int(os.environ.get("HAL_SOUND_RMS_THRESHOLD", "8000"))
SOUND_SAMPLE_DURATION_S = float(os.environ.get("HAL_SOUND_SAMPLE_DURATION_S", "0.5"))

LIGHT_LEVEL_INTERVAL_S = float(os.environ.get("HAL_LIGHT_LEVEL_INTERVAL_S", "300.0"))
LIGHT_CHANGE_THRESHOLD = int(os.environ.get("HAL_LIGHT_CHANGE_THRESHOLD", "100"))

USERS_DIR: str = os.environ.get("HAL_USERS_DIR", "/root/local/users")
STRANGERS_DIR: str = os.environ.get("HAL_STRANGERS_DIR", "/root/local/strangers")
YUNET_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_YUNET_CONFIDENCE_THRESHOLD", "0.35")
)
FACE_COOLDOWN_S = float(os.environ.get("HAL_FACE_COOLDOWN_S", "10.0"))
FACE_OWNER_FORGET_S = float(os.environ.get("HAL_FACE_OWNER_FORGET_S", "3600.0"))
FACE_STRANGER_FORGET_S = float(os.environ.get("HAL_FACE_STRANGER_FORGET_S", "1800.0"))
# Floor between stranger-only presence.enter events (embedding flicker mints new ids).
FACE_STRANGER_ENTER_FLOOR_S = float(os.environ.get("HAL_FACE_STRANGER_ENTER_FLOOR_S", "300.0"))
# Strangers are greeted only after facing the lamp on MIN_FACING of the last TICKS face-ID ticks (#531).
FACE_STRANGER_GAZE_TICKS = int(os.environ.get("HAL_FACE_STRANGER_GAZE_TICKS", "3"))
FACE_STRANGER_GAZE_MIN_FACING = int(
    os.environ.get("HAL_FACE_STRANGER_GAZE_MIN_FACING", "2")
)
# Max age of counted ticks: 3 ticks at the 2 s interval plus slack for a skipped tick.
FACE_STRANGER_GAZE_WINDOW_S = float(
    os.environ.get("HAL_FACE_STRANGER_GAZE_WINDOW_S", "10.0")
)
# Only enrolled faces grant voice focus unless guest-first is opted into.
PRESENCE_WAKE_STRANGERS: bool = (
    os.environ.get("HAL_PRESENCE_WAKE_STRANGERS", "false").lower()
    in ("1", "true", "yes")
)
# Fraction of frame HEIGHT (not area): height is robust to yaw and falls off linearly.
FACE_HEIGHT_RATIO_THRESHOLD = float(
    os.environ.get("HAL_FACE_HEIGHT_RATIO_THRESHOLD", "0.10")
)
# Max fraction of a bbox outside the frame; clipped faces yield hallucinated landmarks.
FACE_MAX_TRUNCATION = float(os.environ.get("HAL_FACE_MAX_TRUNCATION", "0.05"))
# Laplacian variance of the ALIGNED 112x112 crop; calibrated to this camera (blur gate).
FACE_MIN_SHARPNESS = float(os.environ.get("HAL_FACE_MIN_SHARPNESS", "100.0"))
# Consecutive ticks an unrecognised face must persist before a stranger id is minted.
FACE_STRANGER_MIN_TICKS = int(os.environ.get("HAL_FACE_STRANGER_MIN_TICKS", "2"))
# Must span a few sensing ticks (2s each).
FACE_STRANGER_CORROBORATION_S = float(
    os.environ.get("HAL_FACE_STRANGER_CORROBORATION_S", "6.0")
)
# Upload (phone photo) match threshold; sits between the 3/4-pose and frontal clusters (#299).
FACE_MATCH_THRESHOLD = float(os.environ.get("HAL_FACE_MATCH_THRESHOLD", "0.40"))
# Higher bar for matches via the auto-captured extended bank (weaker evidence).
FACE_EXTENDED_THRESHOLD = float(os.environ.get("HAL_FACE_EXTENDED_THRESHOLD", "0.45"))
# Stranger-bank match bar, same as the extended bank (#429).
FACE_STRANGER_THRESHOLD = float(os.environ.get("HAL_FACE_STRANGER_THRESHOLD", "0.45"))
# Min similarity to the enrolled uploads before a view may join the extended bank.
FACE_EXTEND_MIN_ENROLL_SIM = float(
    os.environ.get("HAL_FACE_EXTEND_MIN_ENROLL_SIM", "0.45")
)
# Per-detection face debug capture under FACEID_LOG_DIR (~43 ms/frame when on).
FACEID_DEBUG_LOG_ENABLED = (
    os.environ.get("HAL_FACEID_DEBUG_LOG_ENABLED", "false").lower() == "true"
)
FACEID_LOG_DIR = os.environ.get(
    "HAL_FACEID_LOG_DIR",
    "/opt/hal/drivers/sensing/perceptions/processors/faceid-logs",
)
# 0 = unbounded; 500 is ~15 minutes of one person in frame.
FACEID_LOG_MAX_TRIGGERS = int(os.environ.get("HAL_FACEID_LOG_MAX_TRIGGERS", "500"))

# Far shorter than FACE_OWNER_FORGET_S: a voice proves presence only once.
VOICE_USER_FORGET_S = float(os.environ.get("HAL_VOICE_USER_FORGET_S", "300.0"))

OS_CONFIG_PATH = os.environ.get("OS_CONFIG_PATH", "/root/config/config.json")


def get_tts_speed() -> float:
    """Read the saved rate whenever TTS is created; retain the legacy env fallback."""
    import json

    try:
        with open(OS_CONFIG_PATH) as f:
            speed = json.load(f).get("tts_speed")
        if isinstance(speed, (int, float)) and not isinstance(speed, bool) and 0.25 <= speed <= 4.0:
            return float(speed)
    except (OSError, ValueError, AttributeError):
        pass
    return TTS_SPEED

# Persisted speaker volume (0-100) restored by os-server at boot.
VOLUME_STATE_PATH = os.environ.get(
    "HAL_VOLUME_STATE_PATH", os.path.join(os.path.dirname(OS_CONFIG_PATH), ".volume")
)

def _os_cfg_get(key: str, default: str = "") -> str:
    """Read a value from the os-server config.json (shared with the Go server)."""
    try:
        import json
        with open(OS_CONFIG_PATH) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default

def resolve_device_type(default: str = "") -> str:
    """Return the device class (lamp/dog/intern): DEVICE_TYPE env, then config.json."""
    dev = os.environ.get("DEVICE_TYPE")
    if dev:
        return dev.strip().lower()
    cfg = _os_cfg_get("device_type")
    if cfg:
        return str(cfg).strip().lower()
    return default


DL_BACKEND_URL = _os_cfg_get("llm_base_url") or os.environ.get("DL_BACKEND_URL", "")
DL_API_KEY = _os_cfg_get("llm_api_key") or os.environ.get("DL_API_KEY", "")
# Separate from the LLM key; falls back to it for devices provisioned before the split.
DEVICE_AUTH_TOKEN = (
    _os_cfg_get("device_auth_token")
    or os.environ.get("HAL_DEVICE_AUTH_TOKEN")
    or DL_API_KEY
)
DL_HEARTBEAT_INTERVAL_S = float(os.environ.get("HAL_DL_HEARTBEAT_INTERVAL_S", "60.0"))
# A stalled perception WS would block a shared pool worker; drop and retry.
DL_WS_RECV_TIMEOUT_S = float(os.environ.get("HAL_DL_WS_RECV_TIMEOUT_S", "15.0"))
# One line per stall: <iso_ts>\t<task>\t<detail>.
DL_STALL_LOG_FILE = os.environ.get("HAL_DL_STALL_LOG", "/root/local/dl_ws_stall.log")

DL_ENCRYPTION_ENABLED: bool = os.environ.get("HAL_DL_ENCRYPTION", "true").lower() in ("1", "true", "yes")
DL_ENCRYPTION_REQUIRED: bool = os.environ.get("HAL_DL_ENCRYPTION_REQUIRED", "false").lower() in ("1", "true", "yes")
DL_PUBLIC_KEY_FILE: str = os.environ.get("DL_PUBLIC_KEY_FILE", "")
DL_PUBLIC_KEY_ENDPOINT = os.environ.get("DL_PUBLIC_KEY_ENDPOINT", "/crypto/public-key")
DL_PUBLIC_KEY_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_PUBLIC_KEY_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

DL_MOTION_ENDPOINT = os.environ.get("DL_MOTION_ENDPOINT", "/ws/hal/api/dl/action-analysis/ws")
DL_MOTION_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_MOTION_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_EMOTION_RECOGNIZE_ENDPOINT = os.environ.get("DL_EMOTION_RECOGNIZE_ENDPOINT", "/hal/api/dl/emotion-recognize")
DL_POSE_ENDPOINT = os.environ.get("DL_POSE_ENDPOINT", "/ws/hal/api/dl/pose-estimation/ws")
DL_POSE_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_POSE_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/hal/api/dl/audio-recognizer/embed")
DL_SPEAKER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SER_ENDPOINT: str = os.environ.get("DL_SER_ENDPOINT", "/hal/api/dl/ser/recognize")
DL_SER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

MOTION_ENABLED = os.environ.get("HAL_MOTION_ENABLED", "true").lower() == "true"
MOTION_PER_FACE_ENABLED = os.environ.get("HAL_MOTION_PER_FACE_ENABLED", "false").lower() == "true"
MOTION_PER_FACE_DEDUP_WINDOW_S = float(os.environ.get("HAL_MOTION_PER_FACE_DEDUP_WINDOW_S", "300.0"))
MOTION_PER_FACE_SESSION_TTL_S = float(os.environ.get("HAL_MOTION_PER_FACE_SESSION_TTL_S", "30.0"))
MOTION_PER_FACE_MIN_FRAMES = int(os.environ.get("HAL_MOTION_PER_FACE_MIN_FRAMES", "4"))
MOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_MOTION_CONFIDENCE_THRESHOLD", "0.3")
)
MOTION_FLUSH_S = float(os.environ.get("HAL_MOTION_FLUSH_S", "10.0"))
# Same-activity heartbeat floor; class transitions bypass it.
MOTION_EVENT_COOLDOWN_S = float(
    os.environ.get("HAL_MOTION_EVENT_COOLDOWN_S", "900.0")
)
# Min gap for the transition bypass (guards against flicker).
MOTION_TRANSITION_MIN_GAP_S = float(
    os.environ.get("HAL_MOTION_TRANSITION_MIN_GAP_S", "60.0")
)
MOTION_PERSON_DETECTION_ENABLED = os.environ.get("HAL_MOTION_PERSON_DETECTION_ENABLED", "true").lower() == "true"
MOTION_PERSON_MIN_AREA_RATIO = float(
    os.environ.get("HAL_MOTION_PERSON_MIN_AREA_RATIO", "0.25")
)
MOTION_SNAPSHOT_DIR = os.environ.get(
    "HAL_MOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "hal-motion-snapshots"),
)
MOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("HAL_MOTION_SNAPSHOT_MAX_COUNT", "100"))

EMOTION_ENABLED = os.environ.get("HAL_EMOTION_ENABLED", "true").lower() == "true"
EMOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_EMOTION_CONFIDENCE_THRESHOLD", "0.5")
)
# JSON label -> threshold map, e.g. {"happy":0.5,"sad":0.8}; unset uses the defaults.
EMOTION_LABEL_THRESHOLDS_JSON = os.environ.get("HAL_EMOTION_LABEL_THRESHOLDS", "")
EMOTION_FLUSH_S = float(os.environ.get("HAL_EMOTION_FLUSH_S", "10.0"))
EMOTION_DEDUP_WINDOW_S = float(os.environ.get("HAL_EMOTION_DEDUP_WINDOW_S", "300.0"))
# Stuck-thinking net; 0 disables. 25s clears the p95 delegate wait (22s).
EMOTION_THINKING_RESET_S = float(
    os.environ.get("HAL_EMOTION_THINKING_RESET_S", "25.0")
)
EMOTION_SNAPSHOT_DIR = os.environ.get(
    "HAL_EMOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "hal-emotion-snapshots"),
)
EMOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("HAL_EMOTION_SNAPSHOT_MAX_COUNT", "100"))
# Per-trigger face-emotion debug capture under EMOTION_LOG_DIR.
EMOTION_DEBUG_LOG_ENABLED = (
    os.environ.get("HAL_EMOTION_DEBUG_LOG_ENABLED", "false").lower() == "true"
)
EMOTION_LOG_DIR = os.environ.get(
    "HAL_EMOTION_LOG_DIR",
    "/opt/hal/drivers/sensing/perceptions/processors/emotion-logs",
)
# 0 = unbounded.
EMOTION_LOG_MAX_TRIGGERS = int(os.environ.get("HAL_EMOTION_LOG_MAX_TRIGGERS", "500"))

FIRE_HAZARD_ENABLED = os.environ.get("HAL_FIRE_HAZARD_ENABLED", "true").lower() == "true"
# Min gap between detection API calls (0 would call OWLv2 every ~2s tick).
FIRE_HAZARD_CHECK_INTERVAL_S = float(os.environ.get("HAL_FIRE_HAZARD_CHECK_INTERVAL_S", "5.0"))
FIRE_HAZARD_CONFIDENCE_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_CONFIDENCE_THRESHOLD", "0.3"))
FIRE_HAZARD_OVERLAP_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_OVERLAP_THRESHOLD", "0.2"))
FIRE_HAZARD_CONFIRM_S = float(os.environ.get("HAL_FIRE_HAZARD_CONFIRM_S", "10.0"))
# Re-alert pace for the SAME steady hazard; new types alert immediately.
FIRE_HAZARD_DEDUP_WINDOW_S = float(os.environ.get("HAL_FIRE_HAZARD_DEDUP_WINDOW_S", "1800.0"))
FIRE_HAZARD_FLUSH_S = float(os.environ.get("HAL_FIRE_HAZARD_FLUSH_S", "10.0"))
FIRE_HAZARD_DETECTOR = os.environ.get("HAL_FIRE_HAZARD_DETECTOR", "owlv2")
FIRE_HAZARD_ENDPOINT = os.environ.get("DL_FIRE_HAZARD_ENDPOINT", f"/detect/{FIRE_HAZARD_DETECTOR}")
FIRE_HAZARD_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + FIRE_HAZARD_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
FIRE_HAZARD_API_TIMEOUT_S = float(os.environ.get("HAL_FIRE_HAZARD_API_TIMEOUT_S", "15.0"))

POSE_MOTION_ENABLED = (
    os.environ.get("HAL_POSE_MOTION_ENABLED", "true").lower() == "true"
)
POSE_MOTION_MODEL_PATH = Path(os.environ.get("HAL_POSE_MODEL_PATH", "/root/local/models/rtmpose-m.onnx"))
POSE_MOTION_ANGLE_THRESHOLD = float(
    os.environ.get("HAL_POSE_MOTION_ANGLE_THRESHOLD", "30.0")
)

POSE_ENABLED = os.environ.get("HAL_POSE_ENABLED", "true").lower() == "true"
POSE_ERGO_HIGH_RISK_THRESHOLD = int(os.environ.get("HAL_POSE_ERGO_HIGH_RISK_THRESHOLD", "5"))
# Debug values (30 s / 600 s); production is 60 s / 3600 s.
POSE_SAMPLE_INTERVAL_S = float(os.environ.get("HAL_POSE_SAMPLE_INTERVAL_S", "30.0"))
# Tumbling window; the buffer always resets at its end.
POSE_WINDOW_DURATION_S = float(os.environ.get("HAL_POSE_WINDOW_DURATION_S", "600.0"))
# Skip the nudge when a window has fewer real samples.
POSE_WINDOW_MIN_SAMPLES = int(os.environ.get("HAL_POSE_WINDOW_MIN_SAMPLES", "3"))
# Bad sample: any single region (L or R) at sub-score >= this.
POSE_REGION_HIGH_SUBSCORE = int(os.environ.get("HAL_POSE_REGION_HIGH_SUBSCORE", "4"))
# Fraction of the window that must be bad to attach posture_summary.
POSE_BAD_RATIO = float(os.environ.get("HAL_POSE_BAD_RATIO", "0.6"))
# Kept buckets (bad_ratio >= POSE_BAD_RATIO) survive this long; others are deleted at window close.
POSE_BUCKET_KEEP_S = float(
    os.environ.get("HAL_POSE_BUCKET_KEEP_S", str(2 * 24 * 3600))
)
POSE_SNAPSHOT_MAX_BYTES = int(
    os.environ.get("HAL_POSE_SNAPSHOT_MAX_BYTES", str(50 * 1024 * 1024))
)
# "Worst" samples surfaced from a kept bucket (monitor preview, /dm attach).
POSE_WORST_SNAPSHOTS_PER_BUCKET = int(
    os.environ.get("HAL_POSE_WORST_SNAPSHOTS_PER_BUCKET", "3")
)
# WORKAROUND: perception-service's signed_flexion_angle has the wrong sign; set False once fixed upstream.
POSE_FLIP_DLBACKEND_ANGLE_SIGN = (
    os.environ.get("HAL_POSE_FLIP_DLBACKEND_ANGLE_SIGN", "true").lower() == "true"
)

SNAPSHOT_TMP_DIR = os.environ.get(
    "HAL_SNAPSHOT_TMP_DIR", "/tmp/hal-sensing-snapshots"
)
SNAPSHOT_TMP_MAX_COUNT = int(os.environ.get("HAL_SNAPSHOT_TMP_MAX_COUNT", "50"))
SNAPSHOT_PERSIST_DIR = os.environ.get(
    "HAL_SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots"
)
SNAPSHOT_PERSIST_TTL_S = float(
    os.environ.get("HAL_SNAPSHOT_PERSIST_TTL_S", str(72 * 3600))
)
SNAPSHOT_PERSIST_MAX_BYTES = int(
    os.environ.get("HAL_SNAPSHOT_PERSIST_MAX_BYTES", str(50 * 1024 * 1024))
)

IDLE_TIMEOUT_S = float(os.environ.get("HAL_IDLE_TIMEOUT_S", "300"))
AWAY_TIMEOUT_S = float(os.environ.get("HAL_AWAY_TIMEOUT_S", "900"))
IDLE_BRIGHTNESS = float(os.environ.get("HAL_IDLE_BRIGHTNESS", "0.20"))

SPEAKER_RECOGNITION_ENABLED: bool = (
    os.environ.get("HAL_SPEAKER_RECOGNITION_ENABLED", "true").lower() == "true"
)
SPEAKER_MIN_AUDIO_S: float = float(os.environ.get("HAL_SPEAKER_MIN_AUDIO_S", "0.8")) # seconds
# RAW cosine in [-1, 1] (renamed from the old scaled [0, 1] names; raw = 2 * scaled - 1).
SPEAKER_MATCH_COS: float = float(os.environ.get("SPEAKER_MATCH_COS", "0.5"))
# Extended-set admission band is (SPEAKER_MATCH_COS, SPEAKER_DIVERSITY_COS]; must stay above match.
SPEAKER_DIVERSITY_COS: float = float(os.environ.get("SPEAKER_DIVERSITY_COS", "0.7"))
# SAFETY cap: every extra row raises every speaker's score (false accepts).
SPEAKER_MAX_EXTENDED_SAMPLES: int = int(
    os.environ.get("SPEAKER_MAX_EXTENDED_SAMPLES", "3")
)
# Same cap, applied per unknown-voice cluster.
SPEAKER_MAX_CLUSTER_SAMPLES: int = int(
    os.environ.get("SPEAKER_MAX_CLUSTER_SAMPLES", "3")
)
# WAV files kept per unknown-voice cluster (oldest evicted); also bounds deferred-enrollment rows.
SPEAKER_MAX_CLUSTER_FILES: int = int(
    os.environ.get("HAL_MAX_CLUSTER_FILES", "10")
)
# Extending demands more than recognizing (TV, second speaker, TTS tail).
SPEAKER_EXTEND_MIN_DURATION_SEC: float = float(
    os.environ.get("SPEAKER_EXTEND_MIN_DURATION_SEC", "2.0")
)
SPEAKER_EXTEND_MIN_MARGIN_COS: float = float(
    os.environ.get("SPEAKER_EXTEND_MIN_MARGIN_COS", "0.05")
)
# Purity gates: reject a multi-speaker turn instead of storing a blended mean embedding.
SPEAKER_EXTEND_REQUIRE_UNANIMOUS_CHUNKS: bool = (
    os.environ.get("HAL_SPEAKER_EXTEND_REQUIRE_UNANIMOUS_CHUNKS", "true").lower()
    == "true"
)
# Floor for the weakest winning chunk (defaults to SPEAKER_MATCH_COS).
SPEAKER_EXTEND_MIN_CHUNK_COS: float = float(
    os.environ.get("HAL_SPEAKER_EXTEND_MIN_CHUNK_COS", str(SPEAKER_MATCH_COS))
)
# Enrollment anchors alone must carry the match (audio port of FACE_EXTEND_MIN_ENROLL_SIM).
SPEAKER_EXTEND_MIN_ANCHOR_COS: float = float(
    os.environ.get("HAL_SPEAKER_EXTEND_MIN_ANCHOR_COS", str(SPEAKER_MATCH_COS))
)
SPEAKER_EMBEDDING_API_TIMEOUT_S: float = float(
    os.environ.get("SPEAKER_EMBEDDING_API_TIMEOUT_S", "15")
)
# Rolling cap on incoming_*.wav in the root (~32 KB per audio-second); 0 = unbounded.
SPEAKER_MAX_INCOMING_FILES: int = int(
    os.environ.get("HAL_MAX_INCOMING_FILES", "100")
)
SPEAKER_UNKNOWN_AUDIO_DIR: str = os.environ.get(
    "HAL_UNKNOWN_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "hal-unknown-voice"),
)
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/hal/api/dl/audio-recognizer/embed")
SPEAKER_EMBEDDING_API_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
SPEAKER_EMBEDDING_API_KEY: str = DL_API_KEY

SPEAKER_PROC_TARGET_SR: int = int(os.environ.get("HAL_SPEAKER_PROC_TARGET_SR", "16000"))
SPEAKER_PROC_ENABLE_MONO: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_MONO", "true").lower() == "true"
)
SPEAKER_PROC_ENABLE_RESAMPLE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_RESAMPLE", "true").lower() == "true"
)
SPEAKER_PROC_ENABLE_HIGH_PASS: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_HIGH_PASS", "false").lower() == "true"
)
SPEAKER_PROC_HIGH_PASS_CUTOFF_HZ: float = float(
    os.environ.get("HAL_SPEAKER_PROC_HIGH_PASS_CUTOFF_HZ", "80.0")
)
SPEAKER_PROC_ENABLE_NOISE_REDUCE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_NOISE_REDUCE", "false").lower() == "true"
)
SPEAKER_PROC_NOISE_STATIONARY: bool = (
    os.environ.get("HAL_SPEAKER_PROC_NOISE_STATIONARY", "false").lower() == "true"
)
# TEN-VAD (numpy + onnxruntime).
SPEAKER_PROC_ENABLE_VAD: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_VAD", "true").lower() == "true"
)
SPEAKER_PROC_VAD_MIN_DURATION_SEC: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_MIN_DURATION_SEC", "0.5")
)
# Lowered from 0.4: the band/level gates split segments and lower this ratio.
SPEAKER_PROC_VAD_MIN_VOICE_RATIO: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_MIN_VOICE_RATIO", "0.25")
)
# Onset at this value, offset at (threshold - 0.15); 0.5 is TEN-VAD's operating point.
SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD", "0.5")
)
# Band/level gates drop non-dominant-speaker frames; assume ONE speaker per clip.
SPEAKER_PROC_VAD_SPEAKER_BAND: bool = (
    os.environ.get("HAL_SPEAKER_PROC_VAD_SPEAKER_BAND", "true").lower() == "true"
)
# Empty string disables the level gate.
_vad_max_level_drop_db_raw: str = os.environ.get(
    "HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB", "20.0"
).strip()
SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB: Optional[float] = (
    float(_vad_max_level_drop_db_raw) if _vad_max_level_drop_db_raw else None
)
SPEAKER_PROC_ENABLE_RMS_NORMALIZE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_RMS_NORMALIZE", "true").lower() == "true"
)
SPEAKER_PROC_RMS_TARGET: float = float(
    os.environ.get("HAL_SPEAKER_PROC_RMS_TARGET", "0.1")
)
# SQUIM-STOI intelligibility gate (~20 MB model, downloaded on first use); off: it rejected real speakers.
SPEAKER_PROC_ENABLE_STOI: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_STOI", "false").lower() == "true"
)
SPEAKER_PROC_STOI_MODEL_PATH: str = os.environ.get(
    "HAL_SPEAKER_PROC_STOI_MODEL_PATH",
    "/root/local/models/squimm_stoi.onnx",
)
SPEAKER_PROC_STOI_THRESHOLD: float = float(
    os.environ.get("HAL_SPEAKER_PROC_STOI_THRESHOLD", "0.70")
)
SPEAKER_PROC_STOI_CHUNK_SEC: float = float(
    os.environ.get("HAL_SPEAKER_PROC_STOI_CHUNK_SEC", "5.0")
)

SPEECH_EMOTION_ENABLED: bool = (
    os.environ.get("HAL_SPEECH_EMOTION_ENABLED", "true").lower() == "true"
)
SPEECH_EMOTION_FLUSH_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_FLUSH_S", "10.0")
)
SPEECH_EMOTION_DEDUP_WINDOW_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_DEDUP_WINDOW_S", "300.0")
)
SPEECH_EMOTION_MIN_AUDIO_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_MIN_AUDIO_S", "3.0")
)
SPEECH_EMOTION_API_TIMEOUT_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_API_TIMEOUT_S", "15")
)
DL_SER_ENDPOINT: str = os.environ.get(
    "DL_SER_ENDPOINT", "/hal/api/dl/ser/recognize"
)
SPEECH_EMOTION_API_URL: str = (
    DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/")
    if DL_BACKEND_URL else ""
)
SPEECH_EMOTION_API_KEY: str = DL_API_KEY
SPEECH_EMOTION_AUDIO_DIR: str = os.environ.get(
    "HAL_SPEECH_EMOTION_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "hal-speech-emotion"),
)

# Mirrors the Go agent/factory.go cascade: env > config.json > default.
AGENT_GATEWAY: str = (
    os.environ.get("HAL_AGENT_GATEWAY")
    or _os_cfg_get("agent_runtime")
    or "openclaw"
).strip().lower()
# "remote" is Hermes-over-LAN: same protocol and context manager as "hermes".
if AGENT_GATEWAY == "remote":
    AGENT_GATEWAY = "hermes"

# Precedence per knob: HAL_* env > config.json "realtime" block > default.
# Read once at import: config changes need a HAL restart.
def _os_cfg_realtime() -> dict:
    """The nested 'realtime' dict from os-server config.json, or {} if absent."""
    try:
        import json
        with open(OS_CONFIG_PATH) as f:
            rt = json.load(f).get("realtime")
        return rt if isinstance(rt, dict) else {}
    except Exception:
        return {}


_RT: dict = _os_cfg_realtime()
_RT_GEMINI: dict = _RT.get("gemini") if isinstance(_RT.get("gemini"), dict) else {}
_RT_OPENAI: dict = _RT.get("openai") if isinstance(_RT.get("openai"), dict) else {}
_RT_GPTLIVE: dict = _RT.get("gptlive") if isinstance(_RT.get("gptlive"), dict) else {}
_RT_PIPECAT: dict = _RT.get("pipecat_v1") if isinstance(_RT.get("pipecat_v1"), dict) else {}


def _rt_str(env_key: str, cfg_val, default: str) -> str:
    """Resolve a realtime string knob: env var > config.json value > default."""
    env = os.environ.get(env_key)
    if env:
        return env
    if cfg_val:
        return str(cfg_val)
    return default


def _rt_enabled() -> bool:
    env = os.environ.get("HAL_REALTIME_ENABLED")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    if "enabled" in _RT:
        return bool(_RT["enabled"])
    return True


REALTIME_ENABLED: bool = _rt_enabled()
REALTIME_PROVIDER: str = _rt_str("HAL_REALTIME_PROVIDER", _RT.get("provider"), "gemini")  # none | gemini | openai | gptlive | pipecat_v1
# Gate realtime turns on an STT interim starting with a wake phrase.
WAKEWORD_ENABLED: bool = _os_cfg_get("wakeword", False) is True
# 0 requires the wake phrase for every mic session.
WAKEWORD_FOLLOWUP_TIMEOUT_S: float = max(
    0.0, float(os.environ.get("HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S", "20"))
)
# Max gap between output events before the turn falls back (keep just above first-token latency).
REALTIME_RECV_QUEUE_TIMEOUT_S: float = float(
    os.environ.get("HAL_REALTIME_RECV_QUEUE_TIMEOUT_S", "8.0")
)
# Grace after turn_complete on NON_BLOCKING-tool models for late delegate/reject calls (#453). 0 disables.
REALTIME_NONBLOCKING_TOOL_GRACE_S: float = float(
    os.environ.get("HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S", "6.0")
)
# Bounded from audio commit, not per event.
REALTIME_PROGRESS_TIMEOUT_S: float = max(0.0, float(
    os.environ.get("HAL_REALTIME_PROGRESS_TIMEOUT_S", "15.0")
))
# Hard stop for a silent turn while the server keeps sending (e.g. search grounding). 0 disables.
REALTIME_TURN_MAX_SILENCE_S: float = float(
    os.environ.get("HAL_REALTIME_TURN_MAX_SILENCE_S", "20.0")
)
# Verbose grounding_metadata dump; diagnostic only.
REALTIME_GROUNDING_DEBUG: bool = os.environ.get(
    "HAL_REALTIME_GROUNDING_DEBUG", "false"
).lower() in ("1", "true", "yes")
REALTIME_LOOK_RECV_TIMEOUT_S: float = float(
    os.environ.get("HAL_REALTIME_LOOK_RECV_TIMEOUT_S", "20.0")
)
# Force a fresh session after this many CONSECUTIVE silent turns (zombie guard).
REALTIME_ZOMBIE_RECONNECT_AFTER: int = int(
    os.environ.get("HAL_REALTIME_ZOMBIE_RECONNECT_AFTER", "3")
)
# Cost: recycle the session when a turn follows this much silence. 0 disables.
REALTIME_SESSION_IDLE_RESET_S: float = float(
    os.environ.get("HAL_REALTIME_SESSION_IDLE_RESET_S", "240")
)
# Must stay BELOW the shortest observed idle death (86s).
REALTIME_GEMINI_PRE_TURN_RECYCLE_S: float = float(
    os.environ.get("HAL_GEMINI_PRE_TURN_RECYCLE_S", "60")
)
# Park idle Gemini sessions before the server kills them (WS 1008). Below 86s; 0 disables.
REALTIME_GEMINI_IDLE_PARK_S: float = float(
    os.environ.get("HAL_GEMINI_IDLE_PARK_S", "45")
)
# Fresh-session replays of a turn that produced no output (WS 1011). 0 disables.
REALTIME_GEMINI_TURN_RETRIES: int = int(
    os.environ.get("HAL_GEMINI_TURN_RETRIES", "2")
)
# Cost: recycle after this many turns to cap re-billed context. 0 disables.
REALTIME_SESSION_MAX_TURNS: int = int(
    os.environ.get("HAL_REALTIME_SESSION_MAX_TURNS", "12")
)
# Shorter AND transcript-less captures are VAD false triggers and are not committed.
REALTIME_MIN_COMMIT_DURATION_S: float = float(
    os.environ.get("HAL_REALTIME_MIN_COMMIT_DURATION_S", "0.8")
)
# Re-check empty-STT turns with Silero over the full buffer; fail-open.
REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT: bool = os.environ.get(
    "HAL_REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT", "true"
).lower() in ("1", "true", "yes")
# Voiced-ratio floor for empty-STT turns (real speech >= 0.64, leaked noise 0.30-0.55).
REALTIME_NOISE_SPEECH_RATIO: float = float(
    os.environ.get("HAL_REALTIME_NOISE_SPEECH_RATIO", "0.55")
)
# Never commit an empty-STT turn (Gemini invents a reply); false uses the Silero gate instead.
REALTIME_REQUIRE_TRANSCRIPT: bool = os.environ.get(
    "HAL_REALTIME_REQUIRE_TRANSCRIPT", "true"
).lower() in ("1", "true", "yes")
# Only an explicit `reject_turn` call may suppress the main-agent fallback.
REALTIME_AI_REJECT_FILTER: bool = os.environ.get(
    "HAL_REALTIME_AI_REJECT_FILTER", "true"
).lower() in ("1", "true", "yes")
# Run the voiced-ratio guard on transcripts of at most this many words (STT fabrications). 0 disables.
REALTIME_NOISE_GUARD_MAX_WORDS: int = int(
    os.environ.get("HAL_REALTIME_NOISE_GUARD_MAX_WORDS", "3")
)
# Whole-utterance backchannel words dropped like noise (comma-separated; empty disables).
_FILLERS_DEFAULT = (
    "ok,okay,kay,yeah,yep,yup,uh,uhh,um,umm,uh-huh,uhhuh,mm,mmm,mm-hmm,"
    "mmhmm,mhm,hmm,huh,oh,ah,right,one sec,hold on,hang on"
)
REALTIME_NONACTIONABLE_FILLERS: frozenset[str] = frozenset(
    w.strip().lower()
    for w in os.environ.get("HAL_REALTIME_NONACTIONABLE_FILLERS", _FILLERS_DEFAULT).split(",")
    if w.strip()
)
# Drop replies in a script the device isn't configured for.
REALTIME_FOREIGN_SCRIPT_GUARD: bool = os.environ.get(
    "HAL_REALTIME_FOREIGN_SCRIPT_GUARD", "true"
).lower() in ("1", "true", "yes")
# Live (full-duplex) mode is a whole-process choice: it forces REALTIME_TURN_DETECTION.
LIVE_MODE: bool = os.environ.get("HAL_LIVE_MODE", "false").lower() in (
    "1",
    "true",
    "yes",
)

LIVE_VAD_START_SENSITIVITY: str = os.environ.get(
    "HAL_LIVE_VAD_START_SENSITIVITY", "low"
).strip().lower()
LIVE_VAD_END_SENSITIVITY: str = os.environ.get(
    "HAL_LIVE_VAD_END_SENSITIVITY", ""
).strip().lower()
# 0 = provider default. Speech must persist this long to count as an onset (echo defence).
LIVE_VAD_PREFIX_PADDING_MS: int = int(
    os.environ.get("HAL_LIVE_VAD_PREFIX_PADDING_MS", "300")
)
# 0 = provider default.
LIVE_VAD_SILENCE_MS: int = int(os.environ.get("HAL_LIVE_VAD_SILENCE_MS", "0"))

# "server_vad" | "semantic_vad" | "off"
REALTIME_TURN_DETECTION: str = os.environ.get("HAL_REALTIME_TURN_DETECTION", "off")
if LIVE_MODE and REALTIME_TURN_DETECTION.strip().lower() in ("off", "none", ""):
    REALTIME_TURN_DETECTION = "server_vad"

# Play the model's own audio for realtime-handled turns instead of our TTS.
REALTIME_NATIVE_AUDIO: bool = os.environ.get(
    "HAL_REALTIME_NATIVE_AUDIO", str(_RT.get("native_audio", False))
).lower() in ("1", "true", "yes")

REALTIME_GEMINI_API_KEY: str = (
    os.environ.get("GEMINI_API_KEY", "")
    or os.environ.get("GOOGLE_API_KEY", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
REALTIME_GEMINI_BASE_URL: str = (
    os.environ.get("HAL_GEMINI_LIVE_BASE_URL", "")
    or _RT.get("base_url", "")
    or ((_os_cfg_get("llm_base_url", "").rstrip("/") + "/ws/gemini") if _os_cfg_get("llm_base_url", "") else "")
)
# Default plain 3.8-live (no thinking, BLOCKING tools). Native-audio models re-enable
# the idle workarounds (see gemini_needs_idle_workaround).
REALTIME_GEMINI_MODEL: str = _rt_str("HAL_GEMINI_LIVE_MODEL", _RT_GEMINI.get("model"), "gemini-3.8-live")
REALTIME_GEMINI_VOICE: str = _rt_str("HAL_GEMINI_LIVE_VOICE", _RT_GEMINI.get("voice"), "Kore")
REALTIME_GEMINI_SAMPLE_RATE: int = 16000
REALTIME_GEMINI_THINKING_LEVEL: str = _rt_str("HAL_GEMINI_THINKING_LEVEL", _RT_GEMINI.get("thinking_level"), "LOW")
REALTIME_GEMINI_USE_LANGUAGE_CODES: bool = os.environ.get("HAL_GEMINI_USE_LANGUAGE_CODES", "false").lower() in ("1", "true", "yes")
# Off: the campaign-api proxy does not forward resumption (zombie sessions).
REALTIME_GEMINI_SESSION_RESUMPTION: bool = os.environ.get(
    "HAL_GEMINI_SESSION_RESUMPTION", "false"
).lower() in ("1", "true", "yes")
# Google Search grounding in-session (billed per grounded request).
REALTIME_GEMINI_GOOGLE_SEARCH: bool = (
    os.environ.get(
        "HAL_GEMINI_GOOGLE_SEARCH",
        str(_RT_GEMINI.get("google_search", True)),
    ).lower()
    in ("1", "true", "yes")
)
# Register the `look` tool (one frame per call, downscaled to VISION_MAX_WIDTH).
REALTIME_GEMINI_VISION: bool = (
    os.environ.get(
        "HAL_GEMINI_VISION",
        str(_RT_GEMINI.get("vision", True)),
    ).lower()
    in ("1", "true", "yes")
)
REALTIME_GEMINI_VISION_MAX_WIDTH: int = int(
    os.environ.get("HAL_GEMINI_VISION_MAX_WIDTH", "768")
)
# Aim before `look` captures (yaw only, bounded by LOOK_AIM_DEADLINE_S).
LOOK_AIM_ENABLED: bool = (
    os.environ.get("HAL_LOOK_AIM", "true").lower() in ("1", "true", "yes")
)
# Aim ceiling, only spent on failure (~1s per detect+settle+move iteration).
LOOK_AIM_DEADLINE_S: float = float(
    os.environ.get("HAL_LOOK_AIM_DEADLINE_S", "8")
)
# Look-aim only; separate from tracking's CAMERA_FOV_DEG. Set below the measured 107-123 deg
# (fisheye): undershooting converges, overshooting oscillates.
LOOK_AIM_FOV_DEG: float = float(
    os.environ.get("HAL_LOOK_AIM_FOV_DEG", "100.0")
)
# Stricter than the tracker's 0.15: a false positive turns the lamp at a wall.
LOOK_AIM_MIN_CONFIDENCE: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_CONFIDENCE", "0.5")
)
# Fraction of frame height (asker ~0.23, far colleague ~0.10).
LOOK_AIM_MIN_PERSON_HEIGHT_FRAC: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_PERSON_HEIGHT_FRAC", "0.15")
)
# Faces are smaller than whole persons, so a lower floor.
LOOK_AIM_MIN_FACE_HEIGHT_FRAC: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_FACE_HEIGHT_FRAC", "0.08")
)
# Passively sample where a nearby user sits.
BEARING_SAMPLE_ENABLED: bool = (
    os.environ.get("HAL_BEARING_SAMPLE", "true").lower() in ("1", "true", "yes")
)
# Eight samples reach full confidence within an hour.
BEARING_SAMPLE_INTERVAL_S: float = float(
    os.environ.get("HAL_BEARING_SAMPLE_INTERVAL_S", "300")
)
# Bearing = yaw + dx x scale, only near centre (the FOV is untrusted).
BEARING_SAMPLE_MAX_DX_FRAC: float = float(
    os.environ.get("HAL_BEARING_SAMPLE_MAX_DX_FRAC", "0.25")
)
# Saved under SNAPSHOT_PERSIST_DIR/sensing_bearing/ for review.
BEARING_SNAPSHOT_ENABLED: bool = (
    os.environ.get("HAL_BEARING_SNAPSHOT", "true").lower() in ("1", "true", "yes")
)
# Oldest are evicted past this. One frame per sample interval accumulates
# quietly forever otherwise.
BEARING_SNAPSHOT_KEEP: int = int(
    os.environ.get("HAL_BEARING_SNAPSHOT_KEEP", "30")
)
# Gaze wake: turning toward the lamp as a third wake-gate opener (inert without WAKEWORD_ENABLED).
GAZE_WAKE_ENABLED: bool = (
    os.environ.get("HAL_GAZE_WAKE", "false").lower() in ("1", "true", "yes")
)
# Log decisions without acting (ON by default to calibrate thresholds).
GAZE_WAKE_SHADOW: bool = (
    os.environ.get("HAL_GAZE_SHADOW", "true").lower() in ("1", "true", "yes")
)
# Max head yaw off the lamp that still counts as facing it (profile reads > 60 deg).
GAZE_MAX_YAW_DEG: float = float(os.environ.get("HAL_GAZE_MAX_YAW_DEG", "25"))
# Evidence window reaching back from the newest sample (people turn before they speak).
GAZE_WINDOW_S: float = float(os.environ.get("HAL_GAZE_WINDOW_S", "1.5"))
# Fraction of window samples that must see a facing head (per-sample yaw is noisy).
GAZE_MIN_FACING_RATIO: float = float(
    os.environ.get("HAL_GAZE_MIN_FACING_RATIO", "0.6")
)
# Minimum samples to decide; the loop achieves ~2 samples/s regardless of GAZE_SAMPLE_FPS.
GAZE_MIN_SAMPLES: int = int(os.environ.get("HAL_GAZE_MIN_SAMPLES", "2"))
# Min face height in pixels of the DOWNSCALED frame (VISION_MAX_WIDTH); smaller faces give noise.
GAZE_MIN_FACE_PX: int = int(os.environ.get("HAL_GAZE_MIN_FACE_PX", "48"))
# Cone widening at the frame edge (x GAZE_MAX_YAW_DEG) for lens distortion; 1.0 disables.
GAZE_EDGE_CONE_SCALE: float = float(
    os.environ.get("HAL_GAZE_EDGE_CONE_SCALE", "1.8")
)
# Must exceed GAZE_WINDOW_S.
GAZE_BUFFER_S: float = float(os.environ.get("HAL_GAZE_BUFFER_S", "4.0"))
# 6 fps so enough usable samples reach the vote (measured CPU cost negligible).
GAZE_SAMPLE_FPS: float = float(os.environ.get("HAL_GAZE_SAMPLE_FPS", "6"))
# Minimum gap between two gaze-opened gates.
GAZE_COOLDOWN_S: float = float(os.environ.get("HAL_GAZE_COOLDOWN_S", "5"))
# Follow-up window claimed by a gaze wake; capped by WAKEWORD_FOLLOWUP_TIMEOUT_S.
GAZE_WAKE_FOCUS_S: float = float(os.environ.get("HAL_GAZE_WAKE_FOCUS_S", "10"))
# Boot-scoped sidecar dir (survives HAL restart, not reboot); override for test runs.
STATE_DIR: str = os.environ.get("HAL_STATE_DIR", "/tmp")

# Persistent sleep/wake journal (one JSONL per day) read by the agent.
SLEEP_LOG_DIR: str = os.environ.get("HAL_SLEEP_LOG_DIR", "/root/local/device/sleep")
SLEEP_LOG_MAX_DAYS: int = int(os.environ.get("HAL_SLEEP_LOG_MAX_DAYS", "30"))

# SIM_MEDIA is what was requested; the actual per-subsystem mode lives in app_state.
SIMULATE: bool = os.environ.get("HAL_SIMULATE", "").lower() in ("1", "true", "yes")
SIM_MEDIA: str = os.environ.get("HAL_SIM_MEDIA", "virtual").strip().lower()
# Who a turn belongs to when neither face nor voice has named anyone.
DEFAULT_USER: str = os.environ.get("HAL_DEFAULT_USER", "unknown")
# Survives reboots (not a boot sidecar).
USER_BEARING_PATH: str = os.environ.get(
    "HAL_USER_BEARING_PATH", "/var/lib/hal/user_bearing.json"
)
# Speak while the aim searches for the user.
LOOK_AIM_SPEAK: bool = (
    os.environ.get("HAL_LOOK_AIM_SPEAK", "true").lower() in ("1", "true", "yes")
)
# Announce the capture only when the aim had to move.
LOOK_AIM_SPEAK_CAPTURE: bool = (
    os.environ.get("HAL_LOOK_AIM_SPEAK_CAPTURE", "true").lower() in ("1", "true", "yes")
)
# Min seconds between two `look` image sends; 0 always sends a fresh frame.
REALTIME_GEMINI_VISION_MIN_INTERVAL_S: float = float(
    os.environ.get("HAL_GEMINI_VISION_MIN_INTERVAL_S", "10.0")
)
# Max age of a look frame handed to the main agent; 0 disables.
# MUST stay above REALTIME_LOOK_RECV_TIMEOUT_S plus dispatch time.
REALTIME_GEMINI_VISION_HANDOFF_MAX_AGE_S: float = float(
    os.environ.get("HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S", "45.0")
)

REALTIME_OPENAI_API_KEY: str = (
    os.environ.get("OPENAI_API_KEY", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
REALTIME_OPENAI_BASE_URL: str = (
    os.environ.get("HAL_OPENAI_REALTIME_BASE_URL", "")
    or _RT.get("base_url", "")
    or ((_os_cfg_get("llm_base_url", "").rstrip("/") + "/ws/openai") if _os_cfg_get("llm_base_url", "") else "")
)
REALTIME_OPENAI_MODEL: str = _rt_str("HAL_OPENAI_REALTIME_MODEL", _RT_OPENAI.get("model"), "gpt-realtime-2")
REALTIME_OPENAI_VOICE: str = _rt_str("HAL_OPENAI_REALTIME_VOICE", _RT_OPENAI.get("voice"), "alloy")
REALTIME_OPENAI_SAMPLE_RATE: int = 24000
REALTIME_OPENAI_REASONING_EFFORT: str = _rt_str("HAL_OPENAI_REASONING_EFFORT", _RT_OPENAI.get("reasoning_effort"), "minimal")
# The only source of the user's words on the OpenAI path; mini-transcribe streams deltas.
REALTIME_OPENAI_TRANSCRIBE_MODEL: str = _rt_str(
    "HAL_OPENAI_TRANSCRIBE_MODEL", _RT_OPENAI.get("transcribe_model"), "gpt-4o-mini-transcribe"
)
# "far_field" | "near_field" | "off"
REALTIME_OPENAI_NOISE_REDUCTION: str = _rt_str(
    "HAL_OPENAI_NOISE_REDUCTION", _RT_OPENAI.get("noise_reduction"), "far_field"
).strip().lower()
# 0..1 (API default 0.5); 0 derives it from HAL_LIVE_VAD_START_SENSITIVITY.
REALTIME_OPENAI_VAD_THRESHOLD: float = float(os.environ.get("HAL_OPENAI_VAD_THRESHOLD", "0") or 0)

# GPT-Live (OpenAI /v1/live): a different API from Realtime (see voice_agent/gpt_live.py).
REALTIME_GPTLIVE_API_KEY: str = (
    os.environ.get("OPENAI_API_KEY", "")
    or _RT_GPTLIVE.get("api_key", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
# Falls through to REALTIME_OPENAI_BASE_URL; the SDK appends "/live/sessions".
REALTIME_GPTLIVE_BASE_URL: str = (
    os.environ.get("HAL_GPTLIVE_BASE_URL", "")
    or _RT_GPTLIVE.get("base_url", "")
    or REALTIME_OPENAI_BASE_URL
)
REALTIME_GPTLIVE_MODEL: str = _rt_str("HAL_GPTLIVE_MODEL", _RT_GPTLIVE.get("model"), "gpt-live-1")
REALTIME_GPTLIVE_VOICE: str = _rt_str("HAL_GPTLIVE_VOICE", _RT_GPTLIVE.get("voice"), "marin")
# 16000 or 24000 Hz, one format for both directions (16000 = mic rate, no resample).
REALTIME_GPTLIVE_SAMPLE_RATE: int = int(os.environ.get("HAL_GPTLIVE_SAMPLE_RATE", "16000") or 16000)
# "client" | "responses" | "auto" (responses when web search is on); fixed per session.
REALTIME_GPTLIVE_DELEGATION: str = _rt_str("HAL_GPTLIVE_DELEGATION", _RT_GPTLIVE.get("delegation"), "auto").strip().lower()
# In-session web_search via the Responses backend.
REALTIME_GPTLIVE_WEB_SEARCH: bool = (
    os.environ.get("HAL_GPTLIVE_WEB_SEARCH", str(_RT_GPTLIVE.get("web_search", True))).lower()
    in ("1", "true", "yes")
)
# Backend model for responses delegation.
REALTIME_GPTLIVE_BACKEND_MODEL: str = _rt_str("HAL_GPTLIVE_BACKEND_MODEL", _RT_GPTLIVE.get("backend_model"), "gpt-5.6-luna")
# Output deltas quieter than this (dBFS RMS) are dropped (the stream carries silence).
REALTIME_GPTLIVE_OUTPUT_SILENCE_DBFS: float = float(os.environ.get("HAL_GPTLIVE_OUTPUT_SILENCE_DBFS", "-50") or -50)
# Synthesized turn boundary: output quiet this long ends the reply.
REALTIME_GPTLIVE_TURN_GAP_MS: int = int(os.environ.get("HAL_GPTLIVE_TURN_GAP_MS", "800") or 800)
REALTIME_GPTLIVE_INTERRUPT_GAP_MS: int = int(os.environ.get("HAL_GPTLIVE_INTERRUPT_GAP_MS", "400") or 400)
# Transcript fragments farther apart than this start a new user turn.
REALTIME_GPTLIVE_INPUT_GAP_MS: int = int(os.environ.get("HAL_GPTLIVE_INPUT_GAP_MS", "1500") or 1500)
# Turn-based only: trailing silence appended in place of a commit.
REALTIME_GPTLIVE_COMMIT_SILENCE_MS: int = int(os.environ.get("HAL_GPTLIVE_COMMIT_SILENCE_MS", "600") or 600)
# Wait for the input transcript before forwarding a client delegation.
REALTIME_GPTLIVE_DELEGATION_WAIT_MS: int = int(os.environ.get("HAL_GPTLIVE_DELEGATION_WAIT_MS", "500") or 500)
# Billed per session-minute: park after this much idle time. 0 disables.
REALTIME_GPTLIVE_IDLE_PARK_S: float = float(os.environ.get("HAL_GPTLIVE_IDLE_PARK_S", "30") or 30)

# Pipecat v1: on-device pipeline, text out; needs `uv sync --extra pipecat`.
REALTIME_PIPECAT_API_KEY: str = (
    os.environ.get("HAL_PIPECAT_API_KEY", "")
    or _RT_PIPECAT.get("api_key", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
# Chat-completions URL (not realtime.base_url, which is a WS relay).
REALTIME_PIPECAT_BASE_URL: str = _rt_str(
    "HAL_PIPECAT_BASE_URL",
    _RT_PIPECAT.get("base_url"),
    "https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1",
)
REALTIME_PIPECAT_MODEL: str = _rt_str("HAL_PIPECAT_MODEL", _RT_PIPECAT.get("model"), "qwen/qwen3.6-35b-a3b")
REALTIME_PIPECAT_TEMPERATURE: float = float(os.environ.get("HAL_PIPECAT_TEMPERATURE", "0.7") or 0.7)
REALTIME_PIPECAT_MAX_TOKENS: int = int(os.environ.get("HAL_PIPECAT_MAX_TOKENS", "300") or 300)
# Disable Qwen3 reasoning (dead air); set false for endpoints that reject the extra body.
REALTIME_PIPECAT_DISABLE_THINKING: bool = (
    os.environ.get("HAL_PIPECAT_DISABLE_THINKING", "true").lower() in ("1", "true", "yes")
)
# Fallback STT credentials; VoiceService's provider is reused when present.
REALTIME_PIPECAT_STT_API_KEY: str = os.environ.get("HAL_PIPECAT_STT_API_KEY", "") or _os_cfg_get("llm_api_key", "")
REALTIME_PIPECAT_STT_BASE_URL: str = os.environ.get("HAL_PIPECAT_STT_BASE_URL", "") or _os_cfg_get("llm_base_url", "")
REALTIME_PIPECAT_STT_MODEL: str = os.environ.get("HAL_PIPECAT_STT_MODEL", "") or _os_cfg_get("stt_model", "")
# 16 kHz = the mic's own rate.
REALTIME_PIPECAT_SAMPLE_RATE: int = int(os.environ.get("HAL_PIPECAT_SAMPLE_RATE", "16000") or 16000)
# Live mode turn detection (Silero + Smart Turn v3); thresholds are far-field values.
REALTIME_PIPECAT_SMART_TURN: bool = (
    os.environ.get("HAL_PIPECAT_SMART_TURN", "true").lower() in ("1", "true", "yes")
)
REALTIME_PIPECAT_SMART_TURN_STOP_SECS: float = float(os.environ.get("HAL_PIPECAT_SMART_TURN_STOP_SECS", "3.0") or 3.0)
REALTIME_PIPECAT_VAD_CONFIDENCE: float = float(os.environ.get("HAL_PIPECAT_VAD_CONFIDENCE", "0.85") or 0.85)
REALTIME_PIPECAT_VAD_START_SECS: float = float(os.environ.get("HAL_PIPECAT_VAD_START_SECS", "0.2") or 0.2)
# Short with Smart Turn on: the pause only asks the model "done?".
REALTIME_PIPECAT_VAD_STOP_SECS: float = float(os.environ.get("HAL_PIPECAT_VAD_STOP_SECS", "0.2") or 0.2)
REALTIME_PIPECAT_VAD_MIN_VOLUME: float = float(os.environ.get("HAL_PIPECAT_VAD_MIN_VOLUME", "0.7") or 0.7)
# Smart Turn off: end the turn after this much silence following speech.
REALTIME_PIPECAT_SILENCE_TIMEOUT_S: float = float(os.environ.get("HAL_PIPECAT_SILENCE_TIMEOUT_S", "0.8") or 0.8)
# Words required to open a turn while the model is generating (0 = Pipecat default).
REALTIME_PIPECAT_MIN_WORDS: int = int(os.environ.get("HAL_PIPECAT_MIN_WORDS", "2") or 0)
# Finalize a user turn whose transcript never arrives.
REALTIME_PIPECAT_TURN_STOP_TIMEOUT_S: float = float(os.environ.get("HAL_PIPECAT_TURN_STOP_TIMEOUT_S", "5") or 5)
# Answer the model with an error if the orchestrator never replies.
REALTIME_PIPECAT_TOOL_RESULT_TIMEOUT_S: float = float(os.environ.get("HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S", "15") or 15)
# Client-side `web_search` tool for the pipecat provider (Google-Search relay).
REALTIME_PIPECAT_WEB_SEARCH: bool = (
    os.environ.get(
        "HAL_PIPECAT_WEB_SEARCH",
        str(_RT_PIPECAT.get("web_search", True)),
    ).lower()
    in ("1", "true", "yes")
)
REALTIME_PIPECAT_SEARCH_URL: str = os.environ.get(
    "HAL_PIPECAT_SEARCH_URL",
    "https://campaign-api.autonomous.ai/api/v1/ai/v1/google-search/v1beta/interactions",
)
REALTIME_PIPECAT_SEARCH_MODEL: str = os.environ.get("HAL_PIPECAT_SEARCH_MODEL", "gemini-3.7-flash")
# Same relay, same key as the chat endpoint unless overridden.
REALTIME_PIPECAT_SEARCH_API_KEY: str = os.environ.get("HAL_PIPECAT_SEARCH_API_KEY", "") or REALTIME_PIPECAT_API_KEY
# Must stay below REALTIME_PIPECAT_TOOL_RESULT_TIMEOUT_S.
REALTIME_PIPECAT_SEARCH_TIMEOUT_S: float = float(os.environ.get("HAL_PIPECAT_SEARCH_TIMEOUT_S", "10") or 10)

OPENCLAW_WORKSPACE_DIR: str = os.environ.get("HAL_OPENCLAW_WORKSPACE_DIR", "/root/.openclaw/workspace")
HERMES_WORKSPACE_DIR: str = os.environ.get("HAL_HERMES_WORKSPACE_DIR", "/root/.hermes")
# PicoClaw/Codex/Claude Code/OpenCode workspaces mirror OpenClaw's layout (see orchestrator.py maps).
PICOCLAW_WORKSPACE_DIR: str = os.environ.get("HAL_PICOCLAW_WORKSPACE_DIR", "/root/.picoclaw/workspace")
CODEX_WORKSPACE_DIR: str = os.environ.get("HAL_CODEX_WORKSPACE_DIR", "/root/.codex/workspace")
CLAUDECODE_WORKSPACE_DIR: str = os.environ.get("HAL_CLAUDECODE_WORKSPACE_DIR", "/root/.claudecode/workspace")
OPENCODE_WORKSPACE_DIR: str = os.environ.get("HAL_OPENCODE_WORKSPACE_DIR", "/root/.opencode/workspace")

# The active runtime's workspace; persona readers outside the orchestrator must use it.
_AGENT_WORKSPACE_DIRS: dict[str, str] = {
    "openclaw": OPENCLAW_WORKSPACE_DIR,
    "hermes": HERMES_WORKSPACE_DIR,
    "picoclaw": PICOCLAW_WORKSPACE_DIR,
    "codex": CODEX_WORKSPACE_DIR,
    "claudecode": CLAUDECODE_WORKSPACE_DIR,
    "opencode": OPENCODE_WORKSPACE_DIR,
}
ACTIVE_AGENT_WORKSPACE_DIR: str = _AGENT_WORKSPACE_DIRS.get(
    AGENT_GATEWAY, OPENCLAW_WORKSPACE_DIR
)

# Must sit under the ACTIVE runtime's media root (the agent's image tool allow-list).
_AGENT_CONFIG_DIRS: dict[str, str] = {
    "openclaw": "/root/.openclaw",
    "hermes": "/root/.hermes",
    "picoclaw": "/root/.picoclaw",
    "codex": "/root/.codex",
    "claudecode": "/root/.claudecode",
    "opencode": "/root/.opencode",
}
SNAPSHOT_DIR: str = os.environ.get("HAL_SNAPSHOT_DIR") or (
    _AGENT_CONFIG_DIRS.get(AGENT_GATEWAY, _AGENT_CONFIG_DIRS["openclaw"])
    + "/media/hal-snapshots"
)
# Realtime memory follows the ACTIVE runtime's workspace (never shared across runtimes).
_rt_workspace: str = ACTIVE_AGENT_WORKSPACE_DIR.rstrip("/")
REALTIME_MEMORY_PATH: str = os.environ.get("HAL_REALTIME_MEMORY_PATH", f"{_rt_workspace}/realtime/memory.jsonl")
REALTIME_MAX_MEMORY_ENTRIES: int = int(os.environ.get("HAL_REALTIME_MAX_MEMORY_ENTRIES", "1000"))
REALTIME_MEMORY_TRIM_KEEP: int = int(os.environ.get("HAL_REALTIME_MEMORY_TRIM_KEEP", "500"))
# Per-turn floor sections re-billed every turn (~2k tokens each).
REALTIME_DEVICE_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_DEVICE_MEMORY_MAX_CHARS", "8000"))
REALTIME_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_MEMORY_MAX_CHARS", "8000"))
# Part of the per-turn floor (~1.5k tokens).
REALTIME_SUMMARY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_SUMMARY_MAX_CHARS", "5000"))
# Summarize realtime memory once the verbatim turns in memory.jsonl fill this
# fraction of REALTIME_MEMORY_MAX_CHARS. Turns the loader drops before a
# summary covers them reach no session at all — the long oral test / debate
# that "forgets the rules" at turn ~10 (#449). Below 1.0 so the background
# summarize finishes before the window overflows.
REALTIME_SUMMARIZE_AT_FRACTION: float = float(
    os.environ.get("HAL_REALTIME_SUMMARIZE_AT_FRACTION", "0.75")
)
# Newest turns left verbatim in memory.jsonl by each summarize, so a fresh
# session still has the last exchanges word for word (the question just asked,
# the answer just given) instead of only their paraphrase.
REALTIME_SUMMARY_KEEP_RECENT_TURNS: int = int(
    os.environ.get("HAL_REALTIME_SUMMARY_KEEP_RECENT_TURNS", "4")
)
# Drop stale `## Open requests` before re-feeding the summary (#419, #421). 0 disables.
REALTIME_SUMMARY_OPEN_REQUEST_TTL_S: int = int(os.environ.get("HAL_REALTIME_SUMMARY_OPEN_REQUEST_TTL_S", "3600"))
# Cap on the agent-writable identity section of the floor.
REALTIME_IDENTITY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_IDENTITY_MAX_CHARS", "12000"))
# Cap on the [REPLY] transcript replayed to the main agent.
REALTIME_REPLY_SYNC_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_REPLY_SYNC_MAX_CHARS", "600"))
# Cap on each [TTS HISTORY] line (re-billed until recycle).
REALTIME_TTS_HISTORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_TTS_HISTORY_MAX_CHARS", "300"))
# Seconds of no output before one opening filler is spoken; 0 disables. Tune per device
# from measured time-to-first-sentence.
REALTIME_FILLER_DELAY_S: float = float(os.environ.get("HAL_REALTIME_FILLER_DELAY_S", "1.5"))

# 0 keeps the first sentence intact; a positive cap opts into clause splitting.
REALTIME_FIRST_CHUNK_MAX_CHARS: int = int(
    os.environ.get("HAL_REALTIME_FIRST_CHUNK_MAX_CHARS", "0")
)


# Independent spoken-response routing check; overlaps the tool grace.
REALTIME_OUTCOME_TIMEOUT_S: float = float(os.environ.get("HAL_REALTIME_OUTCOME_TIMEOUT_S", "10"))

REALTIME_SUMMARIZER_ENABLED: bool = os.environ.get("HAL_REALTIME_SUMMARIZER_ENABLED", "true").lower() in ("1", "true", "yes")
REALTIME_SUMMARIZER_API_KEY: str = os.environ.get("HAL_REALTIME_SUMMARIZER_API_KEY", "") or _os_cfg_get("llm_api_key", "")
# Anthropic SDK appends /v1/messages, so strip a trailing /v1.
_summarizer_base: str = os.environ.get("HAL_REALTIME_SUMMARIZER_BASE_URL", "") or _os_cfg_get("llm_base_url", "")
REALTIME_SUMMARIZER_BASE_URL: str = _summarizer_base.rstrip("/").removesuffix("/v1") if _summarizer_base else ""
REALTIME_SUMMARIZER_MODEL: str = os.environ.get("HAL_REALTIME_SUMMARIZER_MODEL", "claude-haiku-4-5-20251001")
# The gateway drops requests intermittently; 0 disables retrying.
REALTIME_SUMMARIZER_RETRIES: int = int(
    os.environ.get("HAL_REALTIME_SUMMARIZER_RETRIES", "2")
)
# Seconds before the first retry; doubled for each one after it.
REALTIME_SUMMARIZER_RETRY_BACKOFF_S: float = float(
    os.environ.get("HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S", "1.5")
)

# Harness announcer (drivers/harness/announcer.py): chance a progress-only snapshot is spoken.
HARNESS_PROGRESS_SPEAK_P: float = float(os.environ.get("HAL_HARNESS_PROGRESS_SPEAK_P", "0.15"))
# At most one spoken progress line per Harness run within this window.
HARNESS_PROGRESS_MIN_GAP_S: float = float(os.environ.get("HAL_HARNESS_PROGRESS_MIN_GAP_S", "60"))
# No spoken progress this soon after the request (the dead-air filler covers it).
HARNESS_PROGRESS_QUIET_START_S: float = float(os.environ.get("HAL_HARNESS_PROGRESS_QUIET_START_S", "15"))
# Progress older than this is never spoken.
HARNESS_PROGRESS_MAX_AGE_S: float = float(os.environ.get("HAL_HARNESS_PROGRESS_MAX_AGE_S", "30"))
# Unspoken results/questions older than this are dropped.
HARNESS_UPDATE_MAX_AGE_S: float = float(os.environ.get("HAL_HARNESS_UPDATE_MAX_AGE_S", "600"))
# Quiet time after speech before the next snapshot.
HARNESS_ANNOUNCE_GRACE_S: float = float(os.environ.get("HAL_HARNESS_ANNOUNCE_GRACE_S", "1.5"))
# Harness text handed to the renderer is cut to this many characters.
HARNESS_ANNOUNCE_CONTENT_MAX_CHARS: int = int(os.environ.get("HAL_HARNESS_ANNOUNCE_CONTENT_MAX_CHARS", "4000"))
# Upper bound on the fallback summarizer call before the sanitized text is spoken instead.
HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S: float = float(os.environ.get("HAL_HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S", "12"))

# Turn back toward the remembered bearing when nobody has been visible for a while
# (the idle loop otherwise walks the camera off the user).
GAZE_REPOINT_ENABLED: bool = (
    os.environ.get("HAL_GAZE_REPOINT", "true").lower() in ("1", "true", "yes")
)
# Nobody well framed for this long before turning.
GAZE_REPOINT_AFTER_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_AFTER_S", "12")
)
# ...and at most this often.
GAZE_REPOINT_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_COOLDOWN_S", "60")
)
# Don't turn while a face was this recently visible.
GAZE_REPOINT_SKIP_IF_FACE_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_SKIP_IF_FACE_S", "3")
)
# Max face offset from centre that still counts as well framed.
GAZE_WELL_FRAMED_EDGE: float = float(
    os.environ.get("HAL_GAZE_WELL_FRAMED_EDGE", "0.6")
)
# Matches aim.MIN_BEARING_CONFIDENCE so every bearing consumer trusts it equally.
GAZE_REPOINT_MIN_CONFIDENCE: float = float(
    os.environ.get("HAL_GAZE_REPOINT_MIN_CONFIDENCE", "0.2")
)
# Wait after a repoint before judging whether anyone was there (feeds the estimate).
GAZE_REPOINT_VERIFY_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_VERIFY_S", "6")
)

# Vertical centring via wrist_pitch (the neck); decreasing the joint tilts the camera UP.
# Open-loop against a coupled arm: the step cap and blind-step budget bound it.
GAZE_PITCH_ENABLED: bool = (
    os.environ.get("HAL_GAZE_PITCH", "true").lower() in ("1", "true", "yes")
)
# Seed, not calibration: the loop re-measures after each step.
GAZE_PITCH_DEG_PER_FRAME: float = float(
    os.environ.get("HAL_GAZE_PITCH_DEG_PER_FRAME", "45")
)
# Largest single correction.
GAZE_PITCH_MAX_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_MAX_STEP_DEG", "15")
)
# Vertical offset (fraction of frame height) that counts as centred.
GAZE_PITCH_DEAD_ZONE_FRAC: float = float(
    os.environ.get("HAL_GAZE_PITCH_DEAD_ZONE_FRAC", "0.15")
)
# Floor between corrections (GAZE_PITCH_WINDOW_S mostly paces them).
GAZE_PITCH_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_COOLDOWN_S", "4")
)
# Median window for the vertical offset; cancels idle's periodic wrist_roll disturbance.
# 6s (half an idle roll cycle) trades some noise for ~4.8s response; use 12 if the head hunts.
GAZE_PITCH_WINDOW_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_WINDOW_S", "6")
)
# Minimum samples to act on a partly-filled window.
GAZE_PITCH_MIN_SAMPLES: int = int(
    os.environ.get("HAL_GAZE_PITCH_MIN_SAMPLES", "8")
)
# Torso readings are a constant -0.5, so few are enough for a direct climb request.
GAZE_PITCH_PROMPT_MIN_SAMPLES: int = int(
    os.environ.get("HAL_GAZE_PITCH_PROMPT_MIN_SAMPLES", "2")
)
# Annotated frame per pitch correction under SNAPSHOT_PERSIST_DIR/sensing_gaze/.
GAZE_SNAPSHOT_ENABLED: bool = (
    os.environ.get("HAL_GAZE_SNAPSHOT", "true").lower() in ("1", "true", "yes")
)
GAZE_SNAPSHOT_KEEP: int = int(os.environ.get("HAL_GAZE_SNAPSHOT_KEEP", "40"))

# Tolerance for a correction to count as landed; stalled joints are rested.
GAZE_PITCH_LAND_TOL_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_LAND_TOL_DEG", "2.0")
)
# Look around once when a repoint finds nobody; the cooldown bounds repetition.
GAZE_SWEEP_ENABLED: bool = (
    os.environ.get("HAL_GAZE_SWEEP", "true").lower() in ("1", "true", "yes")
)
GAZE_SWEEP_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_COOLDOWN_S", "900")
)
# Wait before a self-initiated sweep; longer than GAZE_REPOINT_AFTER_S.
GAZE_SWEEP_AFTER_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_AFTER_S", "30")
)
# Shorter cooldown when there is no bearing at all.
GAZE_SWEEP_COOLDOWN_LOST_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_COOLDOWN_LOST_S", "120")
)

# Climb in fixed steps when a person box touches the top edge (head above frame).
GAZE_FACE_SEARCH_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_FACE_SEARCH_STEP_DEG", "15")
)
# About 60 degrees of climb.
GAZE_FACE_SEARCH_MAX_STEPS: int = int(
    os.environ.get("HAL_GAZE_FACE_SEARCH_MAX_STEPS", "4")
)

# Separate from USER_BEARING_PATH (see face_height.py).
FACE_HEIGHT_PATH: str = os.environ.get(
    "HAL_FACE_HEIGHT_PATH", "/var/lib/hal/face_height.json"
)

# Horizontal (pan) correction, lazier than pitch.
GAZE_YAW_ENABLED: bool = (
    os.environ.get("HAL_GAZE_YAW", "true").lower() in ("1", "true", "yes")
)
GAZE_YAW_WINDOW_S: float = float(os.environ.get("HAL_GAZE_YAW_WINDOW_S", "12"))
GAZE_YAW_MIN_SAMPLES: int = int(os.environ.get("HAL_GAZE_YAW_MIN_SAMPLES", "8"))
# Fraction of full frame width (dx is -0.5..+0.5), applied to the window median.
GAZE_YAW_DEAD_ZONE_FRAC: float = float(
    os.environ.get("HAL_GAZE_YAW_DEAD_ZONE_FRAC", "0.10")
)
# dx is a fraction of frame WIDTH, and the lens spans far more degrees
# horizontally than the correction needs to be aggressive about.
GAZE_YAW_DEG_PER_FRAME: float = float(
    os.environ.get("HAL_GAZE_YAW_DEG_PER_FRAME", "40")
)
GAZE_YAW_MAX_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_YAW_MAX_STEP_DEG", "12")
)
# Neither pan joint fights gravity, so they arrive quickly — but the whole lamp
# turning is a bigger visual event than a head tilt, so it gets the same gentle
# second as the pitch correction rather than the brisk look.aim quarter.
GAZE_YAW_MOVE_S: float = float(os.environ.get("HAL_GAZE_YAW_MOVE_S", "1.0"))

# Gentle, separate from aim.MOVE_DURATION_S (0.25): 15 deg at 1.0s = 15 deg/s.
GAZE_PITCH_MOVE_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_MOVE_S", "1.0")
)
# Let the arm arrive before reading it back.
GAZE_PITCH_SETTLE_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_SETTLE_S", "1.8")
)
# Matched to the measured recovery (~60s).
GAZE_PITCH_STALL_REST_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_STALL_REST_S", "60")
)
# Stop short of the stall point on retry.
GAZE_PITCH_STALL_BACKOFF_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_STALL_BACKOFF_DEG", "2.0")
)
