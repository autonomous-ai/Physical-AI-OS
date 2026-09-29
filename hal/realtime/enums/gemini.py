"""Gemini Live-specific enumerations."""

from enum import StrEnum


class GeminiThinkingLevel(StrEnum):
    MINIMAL = "MINIMAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class GeminiVoice(StrEnum):
    # Same 30 prebuilt voices as Gemini TTS (GeminiTTSBackend.VOICES).
    PUCK = "Puck"
    CHARON = "Charon"
    KORE = "Kore"
    FENRIR = "Fenrir"
    AOEDE = "Aoede"
    ZEPHYR = "Zephyr"
    LEDA = "Leda"
    ORUS = "Orus"
    CALLIRRHOE = "Callirrhoe"
    AUTONOE = "Autonoe"
    ENCELADUS = "Enceladus"
    IAPETUS = "Iapetus"
    UMBRIEL = "Umbriel"
    ALGIEBA = "Algieba"
    DESPINA = "Despina"
    ERINOME = "Erinome"
    ALGENIB = "Algenib"
    RASALGETHI = "Rasalgethi"
    LAOMEDEIA = "Laomedeia"
    ACHERNAR = "Achernar"
    ALNILAM = "Alnilam"
    SCHEDAR = "Schedar"
    GACRUX = "Gacrux"
    PULCHERRIMA = "Pulcherrima"
    ACHIRD = "Achird"
    ZUBENELGENUBI = "Zubenelgenubi"
    VINDEMIATRIX = "Vindemiatrix"
    SADACHBIA = "Sadachbia"
    SADALTAGER = "Sadaltager"
    SULAFAT = "Sulafat"
