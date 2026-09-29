"""Static OpenAPI metadata for the HAL FastAPI app (description + tag docs)."""

API_DESCRIPTION = (
    "Hardware driver API for the OS. "
    "Controls servo motors (5-axis Feetech), RGB LEDs (64x WS2812), "
    "camera, audio (mic/speaker), display, and AI voice pipeline. "
    "OS Server (Go, port 5000) bridges requests here."
)

OPENAPI_TAGS = [
    {
        "name": "Servo",
        "description": "5-axis Feetech servo motor control. Play pre-recorded animations or send direct joint positions.",
    },
    {
        "name": "Policy",
        "description": "Learned robot-control policy interface. Currently dry-run only: requests are logged and never actuate hardware.",
    },
    {
        "name": "LED",
        "description": "WS2812 RGB LED strip (64 LEDs). Set solid color, paint individual pixels, or turn off.",
    },
    {
        "name": "Camera",
        "description": "USB camera for snapshots and MJPEG streaming.",
    },
    {
        "name": "Audio",
        "description": "Low-level audio hardware control. Volume (amixer), raw recording (mic), and test tones. No AI -- just hardware.",
    },
    {
        "name": "Emotion",
        "description": "High-level orchestration: single call coordinates servo animation + LED color + display expression for an emotion.",
    },
    {
        "name": "Scene",
        "description": "Lighting scene presets (reading, focus, relax, movie, night, energize). Sets LED color temperature and brightness.",
    },
    {
        "name": "Presence",
        "description": "PIR motion sensor presence detection. Auto-dims lights when user is idle/away.",
    },
    {
        "name": "Display",
        "description": "Round LCD display: pixel art eye expressions (default) or info mode (time, weather, text).",
    },
    {
        "name": "Voice",
        "description": "AI voice pipeline. Deepgram STT (always-on listening) + LLM-based TTS (text-to-speech). Requires API keys.",
    },
    {
        "name": "Speaker",
        "description": "Per-user voice enrollment + recognition via cosine similarity on external-API embeddings.",
    },
    {
        "name": "System",
        "description": "Health checks and system status.",
    },
]
