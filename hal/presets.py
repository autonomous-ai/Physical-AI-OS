"""HAL presets: emotion, scene, aim and LED constants (pure data)."""

# DEFAULT_LANG is the fallback when stt_language is empty or unknown.
LANG_EN = "en"
LANG_VI = "vi"
LANG_ZH_CN = "zh-CN"
LANG_ZH_TW = "zh-TW"
SUPPORTED_LANGS = [LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW]
DEFAULT_LANG = LANG_EN

# LED state types (tracked in _user_led_state["type"])
LST_SOLID = "solid"
LST_PAINT = "paint"
LST_EFFECT = "effect"
LST_SCENE = "scene"
LST_OFF = "off"

RGB_CMD_SOLID = "solid"
RGB_CMD_PAINT = "paint"

SERVO_CMD_PLAY = "play"
SERVO_CMD_MUSIC_START = "music_start"
SERVO_CMD_MUSIC_STOP = "music_stop"

FX_BREATHING = "breathing"
# Fractional level spread across the ring; readable at very low peaks.
FX_BREATHING_FINE = "breathing_fine"
FX_CANDLE = "candle"
FX_RAINBOW = "rainbow"
FX_NOTIFICATION_FLASH = "notification_flash"
FX_PULSE = "pulse"
FX_BLINK = "blink"
FX_SPEAKING_WAVE = "speaking_wave"
FX_SPEAKING_WAVE_RAINBOW = "speaking_wave_rainbow"

VALID_LED_EFFECTS = [FX_BREATHING, FX_BREATHING_FINE, FX_CANDLE, FX_RAINBOW, FX_NOTIFICATION_FLASH, FX_PULSE, FX_BLINK, FX_SPEAKING_WAVE,
                     FX_SPEAKING_WAVE_RAINBOW]

SCENE_READING = "reading"
SCENE_FOCUS = "focus"
SCENE_RELAX = "relax"
SCENE_MOVIE = "movie"
SCENE_NIGHT = "night"
SCENE_ENERGIZE = "energize"

AIM_CENTER = "center"
AIM_DESK = "desk"
AIM_WALL = "wall"
AIM_LEFT = "left"
AIM_RIGHT = "right"
AIM_UP = "up"
AIM_DOWN = "down"
AIM_USER = "user"

# Each maps to recordings/<name>.csv.
SERVO_CURIOUS = "curious"
SERVO_HAPPY_WIGGLE = "happy_wiggle"
SERVO_SAD = "sad"
SERVO_THINKING_DEEP = "thinking_deep"
SERVO_IDLE = "idle"
SERVO_EXCITED = "excited"
SERVO_SHY = "shy"
SERVO_SHOCK = "shock"
SERVO_LISTENING = "listening"
SERVO_LAUGH = "laugh"
SERVO_CONFUSED = "confused"
SERVO_SLEEPY = "sleepy"
SERVO_GREETING = "greeting"
SERVO_GOODBYE = "goodbye"
SERVO_NOD = "nod"
SERVO_ACKNOWLEDGE = "acknowledge"
SERVO_STRETCHING = "stretching"
SERVO_SCANNING = "scanning"
SERVO_HEADSHAKE = "headshake"
SERVO_WAKE_UP = "wake_up"
SERVO_MUSIC_GROOVE = "music_groove"
SERVO_MUSIC_JAZZ = "music_jazz"
SERVO_MUSIC_CLASSICAL = "music_classical"
SERVO_MUSIC_HIPHOP = "music_hiphop"
SERVO_MUSIC_ROCK = "music_rock"
SERVO_MUSIC_WALTZ = "music_waltz"
SERVO_MUSIC_CHILL = "music_chill"
SERVO_MUSIC_HYPE = "music_hype"

# String values are part of the HTTP API contract (SKILL.md).
EMO_CURIOUS = "curious"
EMO_HAPPY = "happy"
EMO_SAD = "sad"
EMO_THINKING = "thinking"
EMO_IDLE = "idle"
EMO_EXCITED = "excited"
EMO_SHY = "shy"
EMO_SHOCK = "shock"
EMO_LISTENING = "listening"
EMO_LAUGH = "laugh"
EMO_CONFUSED = "confused"
EMO_SLEEPY = "sleepy"
EMO_GREETING = "greeting"
EMO_GOODBYE = "goodbye"
EMO_CARING = "caring"
EMO_ACKNOWLEDGE = "acknowledge"
EMO_STRETCHING = "stretching"
EMO_MUSIC_STRONG = "music_strong"
EMO_MUSIC_CHILL = "music_chill"
EMO_SCAN = "scan"
EMO_NOD = "nod"
EMO_HEADSHAKE = "headshake"

# Emotion -> servo recording + LED color (+ optional effect, camera "on"/"off").
# LED peak budget (same as STATUS_LED_PRESETS): green-dominant hues 12, others 16.
# Dim by scaling the original ratios; keep FX_BLINK speed <= 0.5 (~1.5 Hz).
EMOTION_PRESETS = {
    EMO_CURIOUS: {"servo": SERVO_CURIOUS, "color": [12, 8, 0], "effect": FX_CANDLE, "speed": 0.3, "camera": "on"},
    EMO_HAPPY: {"servo": SERVO_HAPPY_WIGGLE, "color": [12, 9, 1], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_SAD: {"servo": SERVO_SAD, "color": [16, 8, 8], "effect": FX_BREATHING, "speed": 0.4, "camera": "on"},
    EMO_THINKING: {"servo": SERVO_THINKING_DEEP,
                   "color": [6, 12, 4],
                   "effect": FX_PULSE,
                   "speed": 0.3,
                   "camera": "on"},
    EMO_IDLE: {"servo": SERVO_IDLE, "color": [12, 8, 1], "effect": FX_BREATHING, "speed": 0.2},
    EMO_EXCITED: {"servo": SERVO_EXCITED, "color": [12, 8, 12], "effect": FX_CANDLE, "speed": 0.5, "camera": "on"},
    EMO_SHY: {"servo": SERVO_SHY, "color": [16, 7, 2], "effect": FX_BREATHING, "speed": 0.3, "camera": "on"},
    # Same peak as STATUS_LED_PRESETS["ready_flash"]; keep in step.
    EMO_SHOCK: {"servo": SERVO_SHOCK, "color": [12, 12, 12], "effect": FX_NOTIFICATION_FLASH, "speed": 1.0,
                "camera": "on"},
    # Breathing, not pulse: pulse's dark gap reads as an alert on a long cue.
    EMO_LISTENING: {"servo": None,
                    "color": [4, 8, 16],
                    "effect": FX_BREATHING_FINE,
                    "speed": 1.2,
                    # Open at full brightness so the cue is readable the instant it fires.
                    "start_at_peak": True,
                    "camera": "on"},
    EMO_LAUGH: {"servo": SERVO_LAUGH, "color": [12, 8, 1], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_CONFUSED: {"servo": SERVO_CONFUSED, "color": [16, 9, 3], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_SLEEPY: {"servo": SERVO_SLEEPY, "color": [0, 0, 0], "camera": "off", "mic": "off", "speaker": "off"},
    EMO_GREETING: {"servo": SERVO_GREETING, "color": [12, 8, 5], "effect": FX_BREATHING, "speed": 0.3, "camera": "on"},
    EMO_GOODBYE: {"servo": SERVO_GOODBYE, "color": [12, 8, 5], "effect": FX_BREATHING, "speed": 0.5},
    EMO_CARING: {"servo": SERVO_NOD, "color": [12, 8, 6], "effect": FX_BREATHING, "speed": 0.4, "camera": "on"},
    EMO_ACKNOWLEDGE: {"servo": SERVO_ACKNOWLEDGE, "color": [3, 12, 4], "effect": FX_BREATHING, "speed": 0.5,
                      "camera": "on"},
    EMO_STRETCHING: {"servo": SERVO_STRETCHING, "color": [12, 12, 2], "effect": FX_BREATHING, "speed": 0.6,
                     "camera": "on"},
    # FX_RAINBOW ignores "color"; its level is "brightness" (0.0-1.0).
    EMO_MUSIC_STRONG: {"servo": SERVO_MUSIC_ROCK, "color": [8, 12, 8], "effect": FX_RAINBOW, "speed": 1.0,
                       "brightness": 1.0},
    EMO_MUSIC_CHILL: {"servo": SERVO_MUSIC_ROCK, "color": [16, 9, 0], "effect": FX_BREATHING, "speed": 0.3},
    EMO_SCAN: {"servo": SERVO_SCANNING, "color": [5, 12, 3], "effect": FX_PULSE, "speed": 0.3, "camera": "on"},
    EMO_NOD: {"servo": SERVO_NOD, "color": [12, 8, 1], "effect": FX_BREATHING, "speed": 0.5, "camera": "on"},
    EMO_HEADSHAKE: {"servo": SERVO_HEADSHAKE, "color": [16, 6, 1], "effect": FX_BREATHING, "speed": 0.5,
                    "camera": "on"},
}

# Scene presets; comments give the simulated color temperature.
# camera/mic/speaker: "off"/"on"; servo: "hold"; omitted = no change.
SCENE_PRESETS = {
    SCENE_READING: {"brightness": 0.80, "color": [255, 209, 163], "aim": AIM_DESK, "camera": "off", "mic": "on",
                    "speaker": "off", "servo": "hold"},  # ~4000K neutral; mic on for voice wake
    SCENE_FOCUS: {"brightness": 0.70, "color": [255, 214, 170], "aim": AIM_DESK, "camera": "off", "mic": "on",
                  "speaker": "off", "servo": "hold"},  # ~4200K warm-neutral; mic on for voice wake
    SCENE_RELAX: {"brightness": 0.40, "color": [255, 166, 87], "aim": AIM_WALL, "camera": "on", "mic": "on",
                  "speaker": "on"},  # ~2700K warm
    SCENE_MOVIE: {"brightness": 0.15, "color": [255, 147, 51], "aim": AIM_WALL, "camera": "off", "mic": "on",
                  "speaker": "off"},  # ~2400K dim amber
    SCENE_NIGHT: {"brightness": 0.05, "color": [255, 105, 0], "aim": AIM_DOWN, "camera": "off", "mic": "on",
                  "speaker": "off"},  # ~1800K deep amber, blue-free; mic stays on for voice wake
    SCENE_ENERGIZE: {"brightness": 1.00, "color": [255, 228, 206], "aim": AIM_UP, "camera": "on", "mic": "on",
                     "speaker": "on"},  # ~5000K daylight
}

# Normalized -100..100 positions on each joint's calibrated span, NOT angles.
# After a recalibration, re-capture by hand from the Manual Move sliders; never convert.
# left/right are still converted values and known to be wrong.
AIM_PRESETS = {
    AIM_CENTER: {"base_yaw.pos": 0.0, "base_pitch.pos": 25.0, "elbow_pitch.pos": 43.0, "wrist_roll.pos": 10.0,
                 "wrist_pitch.pos": 30.0},
    AIM_DESK: {"base_yaw.pos": 0.6, "base_pitch.pos": 41.0, "elbow_pitch.pos": 42.9, "wrist_roll.pos": 9.2,
               "wrist_pitch.pos": 29.8},
    AIM_WALL: {"base_yaw.pos": 0.6, "base_pitch.pos": 19.0, "elbow_pitch.pos": 42.9, "wrist_roll.pos": 0.0,
               "wrist_pitch.pos": -6.0},
    AIM_LEFT: {"base_yaw.pos": -91.57, "base_pitch.pos": 2.62, "elbow_pitch.pos": 35.5, "wrist_roll.pos": 10.06,
               "wrist_pitch.pos": 52.07},
    AIM_RIGHT: {"base_yaw.pos": 88.36, "base_pitch.pos": 2.62, "elbow_pitch.pos": 35.5, "wrist_roll.pos": 10.06,
                "wrist_pitch.pos": 52.07},
    AIM_UP: {"base_yaw.pos": -0.4, "base_pitch.pos": 39.0, "elbow_pitch.pos": 65.0, "wrist_roll.pos": 8.9,
             "wrist_pitch.pos": 27.9},
    AIM_DOWN: {"base_yaw.pos": 0.0, "base_pitch.pos": 8.0, "elbow_pitch.pos": 15.0, "wrist_roll.pos": 5.0,
               "wrist_pitch.pos": -8.0},
    AIM_USER: {"base_yaw.pos": 0.0, "base_pitch.pos": 26.0, "elbow_pitch.pos": 33.0, "wrist_roll.pos": 10.0,
               "wrist_pitch.pos": -38.0},
}

# os-server status state -> LED look (keys must match system/statusled Go constants).
# Peak budget: green-dominant hues 12, others 16, floor ~8. Tune by eye on a device;
# the light.max_brightness gate only scales peaks up to the ceiling, so dim here.
STATUS_LED_PRESETS = {
    "ota": {"effect": FX_BREATHING, "color": [0, 12, 0], "speed": 3.0},  # green — firmware updating
    # Pulse, not breathing, so an error is distinct from mic_muted.
    "error": {"effect": FX_PULSE, "color": [16, 0, 0], "speed": 1.5},  # red — system error
    "booting": {"effect": FX_BREATHING, "color": [0, 6, 16], "speed": 3.0},  # blue — starting up
    "connectivity": {"effect": FX_BREATHING, "color": [16, 7, 0], "speed": 3.0},  # orange — no internet
    "wifi_connecting": {"effect": FX_BLINK, "color": [0, 6, 16], "speed": 0.5},
    # blue blink: associating with Wi-Fi during POST /api/device/setup
    "hal_down": {"effect": FX_BREATHING, "color": [11, 0, 16], "speed": 3.0},  # purple — HAL unreachable
    "agent_down": {"effect": FX_BREATHING, "color": [0, 12, 12], "speed": 3.0},  # cyan — agent disconnected
    "hardware": {"effect": FX_BREATHING, "color": [12, 12, 0], "speed": 3.0},  # yellow — hardware fault
    "ready_flash": {"effect": FX_NOTIFICATION_FLASH, "color": [12, 12, 12], "speed": 1.0},
    # white: agent ready
    # OTA progress is driven by the bootstrap worker, not the statusled state machine
    "ota_progress": {"effect": FX_BREATHING, "color": [16, 8, 0], "speed": 0.4},  # orange — updating
    "ota_error": {"effect": FX_PULSE, "color": [16, 2, 2], "speed": 1.5},  # red pulse — update failed
    "ota_success": {"effect": FX_NOTIFICATION_FLASH, "color": [0, 12, 4], "speed": 1.0},  # green flash — update ok
    # Persistent fill; the one exception to the peak budget (must be spotted across a room).
    "setup": {"effect": "solid", "color": [16, 16, 16], "speed": 1.0},  # white solid — AP/setup ready
    # HAL-local mic-muted resting indicator; dim but must still read in daylight.
    "mic_muted": {"effect": FX_BREATHING, "color": [10, 0, 0], "speed": 0.8},  # dark red — mic muted
}

# Button hold-warning LEDs per armed tier (thresholds in hal/drivers/button_actions.py).
# A dict so presets.json overrides reach readers at call time.
BUTTON_LED_PRESETS = {
    # Physical Harness toggle confirmation; read at dispatch after device overlay.
    "harness_on": {"effect": FX_BREATHING_FINE, "color": [2, 3, 0], "speed": 0.6},
    "harness_off": {"effect": FX_BLINK, "color": [2, 2, 2], "speed": 1.0, "duration_ms": 300},
    "sleep_warn": {"color": [8, 5, 16]},  # sleepy purple (blinking) — hold 2-5s
    "shutdown_warn": {"color": [16, 0, 0]},  # red (blinking) — hold 5-10s
    "factory_reset": {"color": [16, 0, 0]},  # red (solid) — hold 10s+
}

# Amber-yellow: distinct from the statusled "hardware" cue.
LED_BACKEND_ERROR_FLASH = (12, 9, 0)

# Platform fallback stays dark; each device can override ambient_led.resting.
# OS ambient requests restore; HAL owns this look and the saved user preference.
AMBIENT_RESTING_LED = {"effect": LST_SOLID, "color": [0, 0, 0]}


def ambient_resting_is_dark() -> bool:
    """True when the resting look is black (settle paths clear the strip instead)."""
    return not any(AMBIENT_RESTING_LED.get("color") or [0, 0, 0])
