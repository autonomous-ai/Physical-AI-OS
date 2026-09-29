"""Curated Piper voice catalogue — the list the device offers for download."""

VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

BINARY_URL = (
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
    "piper_linux_aarch64.tar.gz"
)

CATALOG = {
    "en_US-ljspeech-medium": {
        "language": "English (US)",
        "lang_code": "en",
        "path": "en/en_US/ljspeech/medium",
        "license": "public domain",
        "requires_attribution": False,
        "size_mb": 64,
    },
    "en_US-kristin-medium": {
        "language": "English (US) — alt voice",
        "lang_code": "en",
        "path": "en/en_US/kristin/medium",
        "license": "public domain",
        "requires_attribution": False,
        "size_mb": 64,
    },
    "en_US-libritts_r-medium": {
        "language": "English (US) — multi-speaker",
        "lang_code": "en",
        "path": "en/en_US/libritts_r/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 79,
    },
    "vi_VN-vais1000-medium": {
        "language": "Tiếng Việt",
        "lang_code": "vi",
        "path": "vi/vi_VN/vais1000/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 63,
    },
    "es_ES-davefx-medium": {
        "language": "Español",
        "lang_code": "es",
        "path": "es/es_ES/davefx/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "de_DE-thorsten-medium": {
        "language": "Deutsch",
        "lang_code": "de",
        "path": "de/de_DE/thorsten/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "pt_BR-faber-medium": {
        "language": "Português (Brasil)",
        "lang_code": "pt",
        "path": "pt/pt_BR/faber/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "fr_FR-siwis-medium": {
        "language": "Français",
        "lang_code": "fr",
        "path": "fr/fr_FR/siwis/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 63,
    },
}

# The voice a device installs when the operator turns Piper on without picking
# one. Public domain, so shipping it carries no obligation at all.
DEFAULT_VOICE = "en_US-ljspeech-medium"


def voice_urls(name: str):
    """(onnx_url, json_url) for a catalogue entry, or None if not listed."""
    meta = CATALOG.get(name)
    if not meta:
        return None
    base = f"{VOICES_BASE}/{meta['path']}/{name}"
    return f"{base}.onnx", f"{base}.onnx.json"
