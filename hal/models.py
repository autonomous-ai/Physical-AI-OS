"""HAL Pydantic request/response models for all FastAPI endpoints."""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, FiniteFloat


class ServoRequest(BaseModel):
    recording: str

    model_config = {"json_schema_extra": {"examples": [{"recording": "curious"}]}}


class ServoContactLearnRequest(BaseModel):
    """Learn run for the contact stop's per-frame load envelope (arm must move free)."""

    recordings: Optional[list[str]] = Field(
        None, description="Recordings to learn; default every recording except music_*."
    )
    runs: int = Field(2, ge=1, le=5, description="Free plays per recording; the envelope keeps the max.")


class ServoStateResponse(BaseModel):
    available_recordings: list[str]
    current: Optional[str]
    # null = no mode holds the body (key stays present, unlike an older HAL).
    motion_mode: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "available_recordings": [
                        "nod",
                        "curious",
                        "happy_wiggle",
                        "idle",
                        "sad",
                        "excited",
                        "shy",
                        "shock",
                        "listening",
                        "thinking_deep",
                        "laugh",
                        "confused",
                        "sleepy",
                        "greeting",
                        "goodbye",
                        "acknowledge",
                        "stretching",
                        "scanning",
                        "wake_up",
                        "headshake",
                        "music_groove",
                        "music_chill",
                        "music_hype",
                    ],
                    "current": "idle",
                    "motion_mode": None,
                }
            ]
        }
    }


class LEDSolidRequest(BaseModel):
    color: Union[list[int], int]
    transient: bool = Field(
        False,
        description="If true, don't overwrite user LED state (used by Buddy/transient overlays).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"color": [255, 100, 0]},
                {"color": 16711680},
            ]
        }
    }


class LEDOffRequest(BaseModel):
    transient: bool = False


class LEDPaintRequest(BaseModel):
    colors: list[Union[list[int], int]]
    # Interpolate `colors` as gradient stops across the whole strip.
    gradient: bool = False
    transient: bool = False

    model_config = {
        "json_schema_extra": {
            "examples": [{"colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]]}]
        }
    }


class LEDStateResponse(BaseModel):
    led_count: int


class LEDColorResponse(BaseModel):
    led_count: int
    on: bool  # True if ANY pixel is lit — the whole ring is read, not pixel 0
    color: list[int]  # [R, G, B] — the ring's brightest pixel (pixel 0 when uniform)
    hex: str  # e.g. "#ff8800"
    brightness: float  # 0.0–1.0 derived from max channel
    uniform: bool  # False when the pixels differ (dithered effects, partial paints)
    effect: Optional[str]  # running effect name, or null
    scene: Optional[str]  # active scene name, or null


class LEDEffectRequest(BaseModel):
    effect: str = Field(
        ...,
        description="Effect name: breathing, candle, rainbow, notification_flash, pulse, blink",
    )
    color: Optional[list[int]] = Field(
        None, description="Base RGB color for the effect (default: current color)"
    )
    speed: float = Field(
        1.0,
        ge=0.1,
        le=5.0,
        description="Speed multiplier (0.1=slow, 1.0=normal, 5.0=fast)",
    )
    duration_ms: Optional[int] = Field(
        None, ge=100, le=60000, description="Auto-stop after duration (null=indefinite)"
    )
    transient: bool = Field(
        False,
        description="If true, don't overwrite user LED state (used by Buddy/transient overlays).",
    )
    brightness: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Level for effects that generate their own color (rainbow). Ignored by color-driven effects, which take their level from `color`.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"effect": "breathing", "color": [255, 100, 0], "speed": 1.0},
                {"effect": "rainbow", "speed": 0.5, "brightness": 0.05},
                {
                    "effect": "notification_flash",
                    "color": [255, 0, 0],
                    "duration_ms": 3000,
                },
            ]
        }
    }


class LEDStatusRequest(BaseModel):
    state: str = Field(
        ...,
        description="System status state name (booting, error, ota, connectivity, "
        "hal_down, agent_down, hardware, ready_flash) — resolved to a color/effect "
        "via STATUS_LED_PRESETS.",
    )

    model_config = {"json_schema_extra": {"examples": [{"state": "booting"}]}}


class LEDEffectResponse(BaseModel):
    status: str
    effect: str
    speed: float


class StatusResponse(BaseModel):
    status: str


class LEDRestingRequest(BaseModel):
    """Owner's resting LED look: device default, off, or a custom solid colour."""

    mode: Literal["default", "off", "custom"]
    color: Optional[list[int]] = Field(None, description="[R, G, B] 0-255; required for custom.")


class LEDRestingPreviewRequest(BaseModel):
    """Show a candidate resting colour without saving it (live colour pickers)."""

    color: list[int] = Field(..., description="[R, G, B] 0-255.")


class LEDRestingPreviewResponse(BaseModel):
    status: str
    painted: bool  # False when sleep, speech or music owns the strip right now


class LEDRestingResponse(BaseModel):
    mode: str
    color: Optional[list[int]]  # the custom colour, or null
    default: dict  # device preset from presets.json (effect, color, optional speed)
    effective: dict  # look the strip settles on while resting


class ServoPlayResponse(BaseModel):
    """"ok" only when the recording started; else "ignored" plus the reason."""

    status: str
    reason: Optional[str] = None


class PolicyRunRequest(BaseModel):
    """Intent for a learned arm-control policy; execution is not enabled yet."""

    policy: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Policy identifier, for example lerobot/smolvla_base.",
    )
    task: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Natural-language task passed to the policy executor in a future implementation.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"policy": "lerobot/smolvla_base", "task": "pick up the mug"}
            ]
        }
    }


class PolicyRunResponse(BaseModel):
    status: str
    id: str
    policy: str
    task: str
    state: str
    dry_run: bool


class PolicyStatusResponse(BaseModel):
    active: Optional[PolicyRunResponse] = None


class PolicyStopResponse(BaseModel):
    status: str
    id: Optional[str] = None
    dry_run: bool = True


class VolumeRequest(BaseModel):
    volume: int = Field(..., ge=0, le=100, description="Volume percentage 0-100")

    model_config = {"json_schema_extra": {"examples": [{"volume": 75}]}}


class AudioDevicesResponse(BaseModel):
    output_device: Optional[int]
    input_device: Optional[int]
    available: bool


class CameraInfoResponse(BaseModel):
    available: bool
    # True once a frame arrived; False with available=True means missing/undetected hardware.
    has_frame: bool = False
    # Negotiated capture mode; None until the capture loop opened the device.
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float] = None
    disabled: bool = False
    manual_override: bool = False
    zoom: float = 1.0


class CameraZoomRequest(BaseModel):
    zoom: float = Field(..., ge=1.0, le=5.0, description="Digital zoom factor, 1.0 = no zoom")

    model_config = {"json_schema_extra": {"examples": [{"zoom": 2.0}]}}


class EmotionRequest(BaseModel):
    emotion: str = Field(
        ...,
        description="Emotion name: curious, happy, sad, thinking, idle, excited, shy, shock",
    )
    intensity: float = Field(0.7, ge=0.0, le=1.0, description="Intensity 0.0-1.0")

    model_config = {
        "json_schema_extra": {"examples": [{"emotion": "curious", "intensity": 0.8}]}
    }


class EmotionResponse(BaseModel):
    status: str
    emotion: str
    servo: Optional[str]
    led: Optional[list[int]]


class SceneRequest(BaseModel):
    scene: str = Field(
        ..., description="Scene name: reading, focus, relax, movie, night, energize"
    )

    model_config = {"json_schema_extra": {"examples": [{"scene": "reading"}]}}


class SceneResponse(BaseModel):
    status: str
    scene: str
    brightness: float
    color: list[int]


class RealtimeHistoryRequest(BaseModel):
    """A main-agent reply the realtime agent must know about but must not say."""

    text: str = Field(
        ..., min_length=1, max_length=2000, description="Reply text to record as history"
    )


class HarnessUpdateRequest(BaseModel):
    """A Harness result, question or progress line for the announcer to speak."""

    kind: Literal["result", "question", "progress"] = Field(..., description="Update type")
    text: str = Field(..., min_length=1, max_length=20000, description="Raw Harness text")
    turn_id: str = Field("", max_length=200, description="Device run that owns the speech")
    outcome: str = Field("", max_length=40, description="Harness outcome, e.g. completed or failed")


class SpeakRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=2000, description="Text to speak via TTS"
    )
    speed: Optional[float] = Field(None, ge=0.25, le=4.0, description="Speed override for this uncached utterance only")
    voice: str = Field("", description="Override TTS voice for this request (e.g. 'Rachel', 'Brian')")
    interruptible: bool = Field(False, description="If True, can be interrupted by next speech")
    harness_result: bool = Field(False, description="Play a short source cue before a Harness reply")
    provider: Optional[str] = Field(None, description="Override TTS provider: 'openai', 'elevenlabs', 'gemini' or 'piper'")
    tts_api_key: Optional[str] = Field(None, description="API key for provider override")
    tts_base_url: Optional[str] = Field(None, description="Base URL for provider override")
    cached: bool = Field(False, description="Look up WAV cache; render+save on miss")
    prerender: bool = Field(False, description="Render+save to cache without playing (warmup)")
    # Only the agentic runtime's actual reply sets this; hardcoded TTS must leave it False.
    realtime_feedback: bool = Field(
        False, description="Feed this text to the realtime agent as history (agent replies only)"
    )
    # turn_seq is os-server-local order; HAL rejects a delayed request from an older turn.
    turn_id: str = Field("", max_length=200, description="Owning agent turn for /voice/speak-queue")
    turn_seq: int = Field(0, ge=0, description="Monotonic owning-turn order for /voice/speak-queue")

    model_config = {
        "json_schema_extra": {"examples": [{"text": "[laugh] Hey! How are you doing today? I missed you! [sigh] It has been so quiet around here.", "voice": "Rachel"}]}
    }


class MusicPlayRequest(BaseModel):
    query: str = Field(
        ..., min_length=1, max_length=500, description="Song name or search query"
    )
    person: str = Field(
        default="", max_length=64, description="Person name (from face recognition) for per-user history"
    )

    model_config = {
        "json_schema_extra": {"examples": [{"query": "The Calling - Wherever You Will Go", "person": "alice"}]}
    }


class MusicStatusResponse(BaseModel):
    available: bool
    playing: bool
    title: Optional[str] = None
    speaker_muted: bool = False


class VolumeSetResponse(BaseModel):
    """POST /audio/volume reply with the volume actually applied (clamped to SAFETY.md)."""
    status: str
    volume: int
    max_volume: Optional[int] = None


class VolumeResponse(BaseModel):
    control: str
    volume: int
    # SAFETY.md `audio.max_volume` (%), or None when undeclared.
    max_volume: Optional[int] = None


class ServoPositionResponse(BaseModel):
    positions: dict[str, float]


class ServoDetail(BaseModel):
    id: int
    angle: Optional[float]
    online: bool
    error: Optional[str] = None


class ServoStatusResponse(BaseModel):
    servos: dict[str, ServoDetail]


class ServoSearchRequest(BaseModel):
    target: str = Field(
        "person",
        description="What to sweep for. 'person'/'face' use the closest-subject "
                    "policy; any other noun goes to the object detector "
                    "(COCO locally, YOLOWorld open-vocab remotely).",
    )
    exhaustive: bool = Field(
        False,
        description="Survey mode for PEOPLE: visit every look, count sightings, "
                    "return home. For 'scan the room' / 'is anyone else here'. "
                    "Ignored for an object target — a search for a thing always "
                    "stops at the first sighting, centres on it and returns a frame.",
    )


class ServoSearchResponse(BaseModel):
    """What a sweep found, as structured fields (`message` is for reading aloud)."""

    status: str = "ok"
    message: str = Field(
        ..., description="One line the agent can say. Every value in it is also a field."
    )
    found: bool
    target: str = Field(..., description="What the sweep was asked for.")
    kind: Optional[str] = Field(
        None, description="What was actually recognised — 'face' where a person was asked for."
    )
    found_at_yaw: Optional[float] = None
    found_at_roll: Optional[float] = None
    centred: bool = Field(
        False,
        description="Whether the fine correction put the subject in the middle of "
                    "the frame. False still means found — the aim is just off.",
    )
    image_path: Optional[str] = Field(
        None,
        description="Absolute path to the annotated JPEG of the winning frame, in "
                    "the active runtime's media dir. Surfaced to the user as a "
                    "thumbnail; the agent cannot read it.",
    )
    looks_visited: int = 0
    bearings_visited: int = Field(
        0, description="Base positions turned through. Looks and bearings are different quantities."
    )


class ServoDemoResponse(BaseModel):
    """Whether the demo started (it runs and narrates on its own thread)."""

    status: str = "ok"
    started: bool
    waypoints: int = Field(0, description="Legs the demo will walk.")
    reason: str = ""


class ServoHoldOwnerRequest(BaseModel):
    owner: Literal["look"] = Field(
        ..., description="Internal hold owner. Only os-server's look sequence uses it; agents call /servo/hold."
    )


class ServoAimRequest(BaseModel):
    direction: str = Field(
        ...,
        description="Named direction: desk, wall, left, right, up, down, center, user",
    )
    duration: float = Field(
        0.5, ge=0.0, le=10.0, description="Move duration in seconds — stretched automatically when the move would exceed the SAFETY.md speed ceiling"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"direction": "desk"}, {"direction": "left", "duration": 3.0}]
        }
    }


class ServoNudgeRequest(BaseModel):
    yaw: float = Field(0.0, ge=-180.0, le=180.0, description="Relative yaw in degrees (negative=left, positive=right)")
    pitch: float = Field(0.0, ge=-90.0, le=90.0, description="Relative pitch in degrees (negative=down, positive=up)")
    duration: float = Field(0.5, ge=0.0, le=10.0, description="Move duration in seconds — stretched automatically when the move would exceed the SAFETY.md speed ceiling")

    model_config = {
        "json_schema_extra": {
            "examples": [{"yaw": -15}, {"yaw": 30, "pitch": 10, "duration": 1.5}]
        }
    }


class ServoAimResponse(BaseModel):
    status: str
    direction: str
    positions: dict[str, float] = Field(description="Driver-reported positions after the command, not clamped goals; may be cached targets on some drivers, or partial/empty on read failure.")
    requested: dict[str, float]
    errors: Optional[dict[str, str]] = None


class SceneListResponse(BaseModel):
    scenes: list[str]
    active: Optional[str]  # currently active scene name, or null


class PresenceResponse(BaseModel):
    state: str
    enabled: bool
    seconds_since_motion: int
    idle_timeout: int
    away_timeout: int


class FaceEnrollRequest(BaseModel):
    image_base64: str = Field(..., description="Base64-encoded image (JPEG or PNG)")
    label: str = Field(..., min_length=1, max_length=64, description="Person name")
    telegram_username: Optional[str] = Field(None, description="Telegram username of the person")
    telegram_id: Optional[str] = Field(None, description="Telegram user ID for DM targeting")


class FaceEnrollResponse(BaseModel):
    status: str
    label: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    photo_path: str
    enrolled_count: int


class FaceStatusResponse(BaseModel):
    enrolled_count: int
    enrolled_names: list[str]


class FacePersonDetail(BaseModel):
    label: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    photo_count: int
    photos: list[str]  # filenames, e.g. ["1711929600000.jpg"]
    mood_days: list[str] = []  # e.g. ["2026-04-09"]
    wellbeing_days: list[str] = []  # e.g. ["2026-04-10"]
    music_suggestion_days: list[str] = []  # e.g. ["2026-04-17"]
    posture_days: list[str] = []  # e.g. ["2026-05-14"] — RULA ergo alerts + nudges
    audio_history_days: list[str] = []  # e.g. ["2026-04-17"]
    voice_samples: list[str] = []  # files in voice/ — wav samples + metadata.json
    habit_patterns: bool = False  # True if habit/patterns.json exists
    files: list[str] = []  # all non-photo files


class FaceOwnersDetailResponse(BaseModel):
    enrolled_count: int
    persons: list[FacePersonDetail]


class UserInfoResponse(BaseModel):
    name: str
    is_friend: bool
    telegram_id: Optional[str] = None
    telegram_username: Optional[str] = None


class FaceRemoveRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


class FacePhotoRemoveRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    filename: str = Field(..., min_length=1, max_length=128)


class FaceRemoveResponse(BaseModel):
    status: str
    label: str
    enrolled_count: int


class FaceResetResponse(BaseModel):
    status: str
    enrolled_count: int


class SensingResponse(BaseModel):
    running: bool
    poll_interval: float
    last_event_seconds_ago: dict[str, int]
    perceptions: list[dict]
    presence: dict


class DisplayStateResponse(BaseModel):
    mode: str
    hardware: bool
    available_expressions: list[str]


class VoiceStatusResponse(BaseModel):
    voice_available: bool
    voice_listening: bool
    tts_available: bool
    tts_speaking: bool
    tts_detail: Optional[dict] = None
    mic_muted: bool = False
    # Hardware mic switch position; null on devices without it. True blocks /voice/unmute (409).
    hw_mic_switch_muted: Optional[bool] = None


class HealthResponse(BaseModel):
    status: str
    servo: bool
    led: bool
    camera: bool
    audio: bool
    sensing: bool
    environment: bool = False
    voice: bool
    tts: bool
    music: bool
    display: bool
    # null when no `thermal` bound is declared; else {over, temp_c, max_temp_c}.
    thermal: Optional[dict] = None
    # null when the motion driver has no overload cut-off; else {active, retry_in_s,
    # threshold, hold_s, retry_s, trips, cut_complete, pending_off, last_trip, load, peak}.
    servo_overload: Optional[dict] = None


class ServoMoveRequest(BaseModel):
    positions: dict[str, FiniteFloat] = Field(
        ...,
        description=(
            "Joint positions (degrees). Ordered by servo ID: "
            "base_yaw.pos (ID 1, min -90 max 90), "
            "base_pitch.pos (ID 2, min -90 max 90), "
            "elbow_pitch.pos (ID 3, min -90 max 90), "
            "wrist_roll.pos (ID 4, min -90 max 90), "
            "wrist_pitch.pos (ID 5, min -90 max 90). "
            "These bounds are the caller's responsibility. Nothing enforces "
            "them: the only position clamp is the motor bus silently holding "
            "the normalized range, far outside these numbers. Speed IS gated "
            "(SAFETY.md motion.max_speed stretches the duration)."
        ),
    )
    duration: float = Field(
        0.5,
        ge=0.0,
        le=10.0,
        description="Move duration in seconds. 0 = instant jump, >0 = smooth interpolation — stretched automatically when the move would exceed the SAFETY.md speed ceiling",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "positions": {
                        "base_yaw.pos": 0.0,
                        "base_pitch.pos": 10.0,
                        "elbow_pitch.pos": -5.0,
                        "wrist_roll.pos": 0.0,
                        "wrist_pitch.pos": 0.0,
                    },
                    "_comment": "ID1 base_yaw [-90,90] | ID2 base_pitch [-90,90] | ID3 elbow_pitch [-90,90] | ID4 wrist_roll [-90,90] | ID5 wrist_pitch [-90,90]",
                },
                {
                    "positions": {"base_pitch.pos": 5.0, "elbow_pitch.pos": 5.0},
                    "duration": 3.0,
                },
            ]
        }
    }


class ServoMoveResponse(BaseModel):
    status: str
    requested: dict[str, float]
    clamped: Optional[dict[str, float]] = Field(default=None, description="Legacy field; null because drivers do not expose a confirmed clamped target.")
    actual: dict[str, float] = Field(default_factory=dict, description="Driver-reported positions after the command; some drivers fall back to cached targets. Not a guarantee the target was reached.")
    duration: float
    errors: Optional[dict[str, str]] = None


class ServoTrackRequest(BaseModel):
    bbox: Optional[list[int]] = Field(
        None, min_length=4, max_length=4,
        description="Bounding box [x, y, w, h]. If omitted, auto-detect using YOLOWorld.",
    )
    target: Union[str, list[str]] = Field(
        "",
        description=(
            "Object name(s) to track. Pass a single string (e.g. 'cup') or a list "
            "of candidate labels (e.g. ['cup', 'mug', 'coffee cup']) when the caller "
            "is unsure of the exact word — YOLOWorld evaluates all candidates and "
            "the highest-confidence detection is used."
        ),
    )
    max_duration_s: Optional[float] = Field(
        None, gt=0,
        description=(
            "Stop after this many seconds. Capped at HAL_TRACKING_MAX_DURATION_S; "
            "omitted means that cap. A greeting look uses a short value, not a stare."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"target": "cup"},
                {"target": ["cup", "mug", "coffee cup"]},
                {"bbox": [280, 200, 80, 80], "target": "water bottle"},
            ]
        }
    }


class ServoTrackResponse(BaseModel):
    status: str
    tracking: bool
    target: Optional[str] = None
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None


class DisplayEyesRequest(BaseModel):
    expression: str = Field(
        ...,
        description="Expression: neutral, happy, sad, curious, thinking, excited, shy, shock, sleepy, angry, love",
    )
    pupil_x: float = Field(
        0.0, ge=-1.0, le=1.0, description="Pupil X: -1.0 (left) to 1.0 (right)"
    )
    pupil_y: float = Field(
        0.0, ge=-1.0, le=1.0, description="Pupil Y: -1.0 (up) to 1.0 (down)"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"expression": "happy", "pupil_x": 0.0, "pupil_y": 0.0}]
        }
    }


class DisplayInfoRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=20, description="Main text (short, e.g. '14:30')"
    )
    subtitle: str = Field(
        "", max_length=40, description="Subtitle (e.g. 'Good afternoon')"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"text": "14:30", "subtitle": "Good afternoon"}]
        }
    }


class VoiceStartRequest(BaseModel):
    llm_api_key: str = Field(
        ..., min_length=1, description="OpenAI-compatible API key for AutonomousSTT (LLM-as-STT fallback)"
    )
    llm_base_url: str = Field(
        ..., min_length=1, description="OpenAI-compatible base URL for TTS and STT"
    )
    tts_api_key: str = Field(
        "",
        description="TTS provider API key (OpenAI / ElevenLabs). Empty falls back to llm_api_key — for households where TTS and LLM share one credential.",
    )
    stt_api_key: str = Field(
        "",
        description="AutonomousSTT API key. Empty falls back to llm_api_key. Ignored when deepgram_api_key is set.",
    )
    stt_base_url: str = Field(
        "", description="AutonomousSTT base URL. Empty falls back to llm_base_url.",
    )
    tts_base_url: str = Field(
        "", description="TTS provider base URL. Empty falls back to llm_base_url.",
    )
    deepgram_api_key: str = Field(
        "", description="Deepgram API key (optional, falls back to Autonomous STT)"
    )
    tts_voice: str = Field(
        "", description="TTS voice name (optional, defaults to config TTS_VOICE)"
    )
    tts_instructions: str = Field(
        "", description="TTS style/vibe instructions (optional, e.g. 'Speak warmly')"
    )
    tts_provider: str = Field(
        "openai", description="TTS provider: 'openai' (default), 'elevenlabs', 'gemini' or 'piper'"
    )


class TTSConfigRequest(BaseModel):
    """Partial TTS settings for POST /voice/tts/config (only sent fields change)."""

    provider: Optional[str] = None
    voice: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    speed: Optional[float] = None


class VoiceInputModeRequest(BaseModel):
    mode: Literal["automatic", "tap_to_talk"]
    wakeword: bool


class VoiceConfigRequest(BaseModel):
    wake_words: list[str] = Field(..., min_length=1, description="Wake word list (lowercase matched)")
