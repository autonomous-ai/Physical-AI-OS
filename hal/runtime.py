"""HAL hardware runtime: FastAPI server on port 5001 (os-server bridges requests here)."""

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

_startup_started = time.perf_counter()

from dotenv import load_dotenv

# Load .env BEFORE any hal imports so config.py reads the right env vars.
load_dotenv(Path(__file__).parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import hal.app_state as state
from hal.telemetry import tts_hooks
from hal.config import (
    AUDIO_INPUT_ALSA,
    AUDIO_OUTPUT_ALSA,
    AUDIO_SENSING_DEVICE,
    CAMERA_AUTO_EXPOSURE,
    CAMERA_BRIGHTNESS,
    CAMERA_EXPOSURE,
    CAMERA_GAIN,
    CAMERA_HEIGHT,
    CAMERA_INDEX,
    CAMERA_NAME,
    CAMERA_WIDTH,
    DL_API_KEY,
    HTTP_HOST,
    HTTP_PORT,
    DEVICE_ID,
    LOOK_AIM_ENABLED,
    SERVO_FPS,
    SERVO_HOLD_S,
    SERVO_PLAY_RAMP_S,
    SERVO_PORT,
    SIMULATE,
    SIM_MEDIA,
    get_tts_speed,
    TTS_VOICE,
    TTS_INSTRUCTIONS,
    OS_CONFIG_PATH,
)
from hal.models import HealthResponse, StatusResponse
from hal.presets import SERVO_CMD_PLAY, normalize_language
from hal.server_support.openapi_meta import API_DESCRIPTION, OPENAPI_TAGS

from hal.server_support.log_setup import setup_logging

logger = setup_logging()


# Resolve ROBOT.md before driver imports so undeclared hardware costs zero import time.


from hal.server_support.boot_config import (
    _resolve_device_type, _devices_dir, boot_config,
)

_boot = boot_config()
_profile = _boot.profile
_declared = _profile.declared_routes()
_simulation = SIMULATE
_simulation_media = SIM_MEDIA
if _simulation_media not in {"virtual", "host"}:
    raise RuntimeError("HAL_SIM_MEDIA must be 'virtual' or 'host'")
if _simulation:
    logger.info("Simulation mode enabled for device '%s' (media=%s)", _profile.id, _simulation_media)
elif os.environ.get("HAL_BOARD") == "sim":
    raise RuntimeError("HAL_BOARD=sim requires HAL_SIMULATE=1; refusing a physical-driver boot on a virtual board")

# Warm lerobot -> torch (~4s) in parallel; per-module import locks make the later import a join.
if "servo" in _declared:
    import importlib
    from hal.drivers.motors.factory import MOTION_DRIVERS

    _motion_cap = _profile.capabilities.get("motion")
    _motion_driver = "mock" if _simulation else (_motion_cap.driver if _motion_cap else None)
    _motion_entry = MOTION_DRIVERS.get(_motion_driver or "feetech")

    def _warm_import_servo():
        if _motion_entry:
            try:
                importlib.import_module(_motion_entry[0])
            except Exception:
                pass  # the factory import below reports the real error

    threading.Thread(target=_warm_import_servo, daemon=True, name="warm-import-servo").start()


AnimationService = None  # resolved motion service class (may be any MotionService impl)
RGBService = None
sd = None
np = None


if "led" in _declared:
    try:
        from hal.drivers.rgb.rgb_service import RGBService
    except ImportError as e:
        logger.warning(f"LED drivers not available: {e}")
else:
    logger.info("LED drivers skipped — 'led' not declared in ROBOT.md")

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    logger.warning(f"Audio drivers not available: {e}")

cv2 = None
LocalVideoCaptureDevice = None
VideoCaptureDeviceInfo = None
resolve_camera_device_id = None
if "camera" in _declared:
    try:
        import cv2
    except ImportError as e:
        logger.warning(f"Camera drivers (opencv) not available: {e}")

    try:
        from hal.drivers.camera.factory import resolve_camera_class
        from hal.drivers.camera.models import VideoCaptureDeviceInfo
        from hal.drivers.camera.video_capture_device import resolve_camera_device_id

        # ROBOT.md picks the vision backend (CSI/libcamera vs UVC).
        _vision_cap = _profile.capabilities.get("vision")
        # Simulation never uses the production UVC driver ("virtual" or "host" webcam).
        if _simulation:
            _camera_driver = "host" if _simulation_media == "host" else "virtual"
        else:
            _camera_driver = _vision_cap.driver if _vision_cap else None
        LocalVideoCaptureDevice = resolve_camera_class(
            _camera_driver,
            _vision_cap.required if _vision_cap else False,
        )
        if LocalVideoCaptureDevice is None:
            logger.warning("Camera capture device not available (driver: %s)",
                           _vision_cap.driver if _vision_cap else None)
    except ImportError as e:
        logger.warning(f"Video capture device not available: {e}")
else:
    logger.info("Camera drivers skipped — 'camera' not declared in ROBOT.md")

# Vendor runtimes that hold the camera/audio declare `owner:`; release each distinct owner once.
_media_owners = []
for _owner_name in dict.fromkeys(
    c.owner for c in _profile.capabilities.values() if c.owner
):
    from hal.drivers.media_owner.factory import resolve_media_owner

    _owner_cls = resolve_media_owner(_owner_name)
    if _owner_cls is not None:
        # Releasing the media resets the card mixer, so the owner needs this body's level.
        _media_owners.append(_owner_cls(startup_volume=_profile.startup_volume))
        logger.info("Media owner '%s' declared — HAL will borrow the hardware", _owner_name)

SensingService = None
FacePerception = None
if "sensing" in _declared:
    try:
        from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import FacePerception
        from hal.drivers.sensing.sensing_service import SensingService
    except ImportError as e:
        logger.warning(f"Sensing service not available: {e}")
        SensingService = None
        FacePerception = None
else:
    logger.info("Sensing service skipped — 'sensing' not declared in ROBOT.md")

if _simulation and "sensing" in _declared:
    from hal.drivers.sensing.virtual_service import VirtualSensingService
    SensingService = VirtualSensingService

VoiceService = None
DeepgramSTT = None
AutonomousSTT = None
TTSService = None
PROVIDER_OPENAI = "openai"  # fallback when the TTS import below is skipped/unavailable
if "voice" in _declared:
    try:
        from hal.drivers.voice.stt import AutonomousSTT
        from hal.drivers.voice.stt import DeepgramSTT
        from hal.drivers.voice.voice_service import VoiceService
    except ImportError as e:
        logger.warning(f"Voice service not available: {e}")
else:
    logger.info("Voice service skipped — 'voice' not declared in ROBOT.md")

# TTS serves more than the voice route (music backchannel, sensing, shutdown cue).
if {"voice", "audio", "music"} & set(_declared):
    try:
        from hal.drivers.voice.tts import TTSService
        from hal.drivers.voice.tts import PROVIDER_OPENAI
    except ImportError as e:
        logger.warning(f"TTS service not available: {e}")

MusicService = None
if "music" in _declared:
    try:
        from hal.drivers.voice.music_service import MusicService
    except ImportError as e:
        logger.warning(f"Music service not available: {e}")

DisplayService = None
if "display" in _declared:
    try:
        from hal.drivers.display.display_service import DisplayService
    except ImportError as e:
        logger.warning(f"Display service not available: {e}")

# Join the motion import late to avoid serializing startup (still before mount checks).
_motion_wait_started = time.perf_counter()
if "servo" in _declared:
    from hal.drivers.motors.factory import resolve_motion_class
    _motion_cap = _profile.capabilities.get("motion")
    AnimationService = resolve_motion_class(
        _motion_driver,
        _motion_cap.required if _motion_cap else False,
    )
    if AnimationService is None:
        logger.warning("Servo motion service not available (driver: %s)",
                       _motion_cap.driver if _motion_cap else None)
else:
    logger.info("Servo drivers skipped — 'servo' not declared in ROBOT.md")

logger.info("[startup] driver_imports_complete elapsed_ms=%.0f motion_wait_ms=%.0f",
            (time.perf_counter() - _startup_started) * 1000,
            (time.perf_counter() - _motion_wait_started) * 1000)

_gpio_button_handlers = []
_ttp223_handler = None
_mpr121_handler = None
_privacy_button_handler = None

# Late async initializers check this so they don't start services nobody will stop.
_lifespan_stopping = threading.Event()


def _sim_audio_probe(sd_module) -> None:
    """Confirm the host speaker and microphone are actually usable (sim only; macOS permissions)."""
    import platform

    def _mac_hint(what: str) -> str:
        if platform.system() != "Darwin":
            return ""
        return (
            f" Grant {what} access to the terminal app running HAL: "
            f"System Settings > Privacy & Security > {what}, then restart "
            f"`make sim SIM_MEDIA=host`."
        )

    if state.audio_output_device is None:
        state.sim_media_fallback("audio", "no host output (speaker) device found")
        state.audio_output_device = 0
        state.audio_input_device = 0
        return
    if state.audio_input_device is None:
        state.sim_media_fallback(
            "audio", "no host input (microphone) device found" + _mac_hint("Microphone")
        )
        state.audio_output_device = 0
        state.audio_input_device = 0
        return
    try:
        rate = int(sd_module.query_devices(state.audio_input_device)["default_samplerate"])
        rec = sd_module.rec(
            max(1, rate // 20),
            samplerate=rate,
            channels=1,
            dtype="int16",
            device=state.audio_input_device,
        )
        sd_module.wait()
        if rec is None:
            raise RuntimeError("microphone returned no samples")
    except Exception as e:
        state.sim_media_fallback(
            "audio", f"microphone unusable: {e}" + _mac_hint("Microphone")
        )
        state.audio_output_device = 0
        state.audio_input_device = 0
        return
    logger.info(
        "Host media: speaker device=%s, microphone device=%s",
        state.audio_output_device,
        state.audio_input_device,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gpio_button_handlers, _ttp223_handler, _mpr121_handler, _privacy_button_handler

    _lifespan_started = time.perf_counter()
    logger.info("[startup] lifespan_begin elapsed_ms=%.0f",
                (_lifespan_started - _startup_started) * 1000)

    # Keep privacy-controlled peripherals closed until the GPIO boot sync.
    from hal import privacy
    privacy.prepare(_privacy_button_config)

    # Phase 0 (blocking, first): borrow hardware from a declared owner before anything opens it.
    for _owner in _media_owners:
        _owner.release()

    # Phase 1: slow hardware init in background threads.

    def _init_servo():
        if not AnimationService:
            return
        # Only when the device declares `motion`; a missing bus makes every playback throw.
        if "servo" not in _plan.mounted:
            logger.info("AnimationService skipped — device does not declare 'motion' (servo route not mounted)")
            return
        try:
            if (_motion_driver or "feetech") == "feetech":
                svc = AnimationService(
                    port=SERVO_PORT, lamp_id=DEVICE_ID, fps=SERVO_FPS,
                    duration=SERVO_PLAY_RAMP_S, hold_s=SERVO_HOLD_S,
                    safety_policy=_safety, geometry=_geometry,
                )
            else:
                # SDK backends carry the safety policy themselves.
                svc = AnimationService(safety_policy=_safety)
            # A device that was asleep must not perform its wake sequence on restart.
            svc.start(skip_wake=state._sleeping)
            state.animation_service = svc
            logger.info("Motion service started (%s)", type(svc).__name__)
        except Exception as e:
            logger.warning(f"Motion service failed to start: {e}")

    def _init_led():
        # The HTTP entrypoint may already own the strip; never reopen it.
        if state.rgb_service is not None:
            return
        if not RGBService:
            return
        try:
            svc = RGBService(led_count=_led_count, safety_policy=_safety)
            svc.start()
            state.rgb_service = svc
            logger.info("RGBService started")
        except Exception as e:
            logger.warning(f"RGBService failed to start: {e}")

    def _init_camera():
        if not (LocalVideoCaptureDevice and VideoCaptureDeviceInfo and cv2):
            return
        # Only when the device declares `vision`.
        if "camera" not in _plan.mounted:
            logger.info("Camera skipped — device does not declare 'vision' (camera route not mounted)")
            return
        try:
            # V4L2 index resolution is a UVC concern only.
            camera_device_id = (
                resolve_camera_device_id(CAMERA_NAME, CAMERA_INDEX)
                if LocalVideoCaptureDevice.requires_v4l2_index
                else CAMERA_INDEX
            )
            cap = LocalVideoCaptureDevice(
                VideoCaptureDeviceInfo(
                    device_id=camera_device_id,
                    max_width=CAMERA_WIDTH,
                    max_height=CAMERA_HEIGHT,
                    auto_exposure=CAMERA_AUTO_EXPOSURE,
                    exposure=CAMERA_EXPOSURE,
                    gain=CAMERA_GAIN,
                    brightness=CAMERA_BRIGHTNESS,
                )
            )
            if _privacy_button_config and _privacy_button_config.disable_camera_on_mute:
                cap = privacy.GuardedCamera(cap)
            if state._camera_disabled:
                # Disabled (restored): create the capture object but don't open the sensor.
                logger.info("Camera capture created but not started -- disabled (restored)")
            else:
                cap.start()
            state.camera_capture = cap
            logger.info(
                f"Camera opened (device={camera_device_id}, {CAMERA_WIDTH}x{CAMERA_HEIGHT})"
            )
        except Exception as e:
            if _simulation and _simulation_media == "host":
                # Fall back to the virtual scene and record why (GET /simulator/state).
                state.sim_media_fallback("camera", str(e))
                try:
                    from hal.drivers.camera.virtual_capture_device import (
                        VirtualVideoCaptureDevice,
                    )

                    cap = VirtualVideoCaptureDevice(
                        VideoCaptureDeviceInfo(
                            device_id=CAMERA_INDEX,
                            max_width=CAMERA_WIDTH,
                            max_height=CAMERA_HEIGHT,
                        )
                    )
                    if not state._camera_disabled:
                        cap.start()
                    state.camera_capture = cap
                    logger.info("Camera fell back to the virtual capture device")
                    return
                except Exception as e2:
                    logger.warning(f"Virtual camera fallback failed: {e2}")
            logger.warning(f"Camera failed to start: {e}")

    hw_threads = []
    for fn in (_init_servo, _init_led, _init_camera):
        t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
        t.start()
        hw_threads.append(t)

    # Phase 2: audio detect + TTS + VoiceService.

    # Without declared audio routes, never enumerate the host's speaker or mic.
    if _simulation and _simulation_media != "host" and {"audio", "voice", "music", "speaker"} & set(_plan.mounted):
        # Virtual device ids are never passed to sounddevice.
        state.audio_output_device = 0
        state.audio_input_device = 0
        logger.info("Audio using virtual input/output devices")
    elif sd and {"audio", "voice", "music", "speaker"} & set(_plan.mounted):
        _audio_results = [None, None]

        def _detect_output():
            _audio_results[0] = state._find_audio_device(output=True)

        def _detect_input():
            _audio_results[1] = state._find_audio_device(output=False)

        _t_out = threading.Thread(target=_detect_output, daemon=True)
        _t_in = threading.Thread(target=_detect_input, daemon=True)
        _t_out.start()
        _t_in.start()
        _t_out.join()
        _t_in.join()

        state.audio_output_device, state.audio_input_device = _audio_results
        _out_env = os.environ.get("HAL_AUDIO_OUTPUT_DEVICE")
        if _out_env is not None:
            state.audio_output_device = int(_out_env)
            logger.info("Audio output device override from env: %d", state.audio_output_device)
        elif os.environ.get("HAL_AUDIO_OUTPUT_ALSA"):
            _alsa_out = os.environ["HAL_AUDIO_OUTPUT_ALSA"]
            _alsa_card = _alsa_out.split(":")[1].split(",")[0] if ":" in _alsa_out else ""
            if _alsa_card:
                # ALSA card ids and PortAudio labels differ by dashes/underscores; normalize both.
                def _norm(s: str) -> str:
                    return "".join(c for c in s.lower() if c.isalnum())

                _needle = _norm(_alsa_card)
                # PortAudio caches devices at import; re-enumerate until the codec alias appears (~10s).
                _matched = False
                for _attempt in range(20):
                    for _i, _d in enumerate(sd.query_devices()):
                        if _needle in _norm(_d["name"]) and _d["max_output_channels"] > 0:
                            state.audio_output_device = _i
                            logger.info(
                                "Audio output device from ALSA env: %d '%s' (matched '%s', attempt=%d)",
                                _i, _d["name"], _alsa_card, _attempt + 1,
                            )
                            _matched = True
                            break
                    if _matched:
                        break
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        logger.exception("sounddevice reinit failed")
                    time.sleep(0.5)
                if not _matched:
                    logger.warning(
                        "ALSA env '%s' never enumerated by PortAudio after 10s; "
                        "TTS will use _find_audio_device fallback (likely silent)",
                        _alsa_out,
                    )
        if state.audio_output_device is not None:
            logger.info(f"Audio output device: {state.audio_output_device}")
        if state.audio_input_device is not None:
            logger.info(f"Audio input device: {state.audio_input_device}")
        if _simulation and _simulation_media == "host":
            _sim_audio_probe(sd)
    elif _simulation and _simulation_media == "host" and {
        "audio", "voice", "music", "speaker"
    } & set(_plan.mounted):
        state.sim_media_fallback(
            "audio", "sounddevice is not installed in this environment"
        )
        state.audio_output_device = 0
        state.audio_input_device = 0

    # Gated on simulation_audio, not _simulation: host mode uses the real pipeline.
    if state.simulation_audio and "voice" in _plan.mounted:
        from hal.drivers.voice.virtual_service import VirtualTTSService, VirtualVoiceService
        state.tts_service = VirtualTTSService(voice=TTS_VOICE, instructions=TTS_INSTRUCTIONS)
        state.voice_service = VirtualVoiceService(tts_service=state.tts_service)
        logger.info("Voice using virtual microphone and speaker")

    os_config_path = OS_CONFIG_PATH
    try:
        with open(os_config_path) as f:
            os_cfg = json.load(f)
        dgk = os_cfg.get("deepgram_api_key", "")
        llm_key = os_cfg.get("llm_api_key", "")
        llm_url = os_cfg.get("llm_base_url", "")
        # Per-service credentials fall back to the brain's (ElevenLabs appends /elevenlabs to its base).
        tts_key = os_cfg.get("tts_api_key", "") or llm_key
        tts_url = os_cfg.get("tts_base_url", "") or llm_url
        stt_key = os_cfg.get("stt_api_key", "") or llm_key
        stt_url = os_cfg.get("stt_base_url", "") or llm_url
        voice = os_cfg.get("tts_voice", "") or TTS_VOICE
        tts_provider = os_cfg.get("tts_provider", PROVIDER_OPENAI)
        if tts_key and tts_url and TTSService and not state.tts_service:
            state.tts_service = TTSService(
                api_key=tts_key,
                base_url=tts_url,
                sound_device_module=sd,
                numpy_module=np,
                output_device=state.audio_output_device,
                voice=voice,
                speed=get_tts_speed(),
                instructions=os_cfg.get("tts_instructions", "") or TTS_INSTRUCTIONS or None,
                on_speak_start=state._on_tts_speak_start,
                on_speak_end=state._on_tts_speak_end,
                provider=tts_provider,
                # Fired at the first frame that reaches the stream (unlike on_speak_start).
                on_playback_audio=tts_hooks.on_playback_audio,
                on_playback_done=tts_hooks.on_playback_done,
                on_playback_muted=tts_hooks.on_playback_muted,
            )
            logger.info(
                "TTSService auto-started (provider=%s, output_device=%s, available=%s)",
                tts_provider,
                state.audio_output_device,
                state.tts_service.available,
            )
        if VoiceService and not state.voice_service:
            agent_name = state._read_agent_name()
            wake_words = state._build_wake_words(agent_name)
            stt_provider = None
            logger.info("STT selection: deepgram_key=%s, DeepgramSTT=%s, AutonomousSTT=%s, agent=%s",
                        bool(dgk), DeepgramSTT is not None, AutonomousSTT is not None, agent_name)
            stt_keywords = state._stt_boost_terms()
            stt_model = (os_cfg.get("stt_model") or "").strip() or None
            stt_language = normalize_language(os_cfg.get("stt_language")) or None
            if dgk and DeepgramSTT:
                from hal.drivers.voice.stt.autonomous import model_for_language

                stt_provider = DeepgramSTT(
                    api_key=dgk, keywords=stt_keywords, language=stt_language,
                    model=stt_model or model_for_language(stt_language),
                )
            elif stt_key and stt_url and AutonomousSTT:
                stt_kwargs = {}
                if stt_model:
                    stt_kwargs["model"] = stt_model
                if stt_language:
                    stt_kwargs["language"] = stt_language
                stt_provider = AutonomousSTT(
                    api_key=stt_key, base_url=stt_url,
                    keywords=stt_keywords, **stt_kwargs
                )
            if stt_provider:
                from hal.drivers.voice.stt.lang_switch import wrap_with_language_id

                stt_provider = wrap_with_language_id(
                    stt_provider, os_cfg.get("stt_languages"), stt_language,
                )
                state.voice_service = VoiceService(
                    stt_provider=stt_provider,
                    input_device=state.audio_input_device,
                    tts_service=state.tts_service,
                    music_service=state.music_service,
                    wake_words=wake_words,
                    alsa_device=AUDIO_INPUT_ALSA,
                    # A mic alone enables voice people perception (speaker-ID, speech emotion).
                    enable_people_perception=("audio" in _profile.capabilities),
                    # No face (`expression`) = no express_emotion tool.
                    enable_expression=("expression" in _profile.capabilities),
                )
                if state._mic_muted:
                    # Muted: build the pipeline but don't open the mic.
                    logger.info("VoiceService created but NOT started -- mic muted")
                else:
                    state.start_voice_service("boot-autostart")
                    logger.info("VoiceService auto-started (%s, wake_words=%s)", stt_provider.name, wake_words)
    except FileNotFoundError:
        logger.info(
            f"os-server config not found at {os_config_path}, voice will wait for /voice/start"
        )
    except Exception as e:
        logger.warning(f"Auto-start voice from os-server config failed: {e}")

    if MusicService:
        try:
            from hal.routes.music import _on_music_complete

            state.music_service = MusicService(on_complete=_on_music_complete)
            if state.tts_service:
                state.music_service._tts_service = state.tts_service
            if state.voice_service:
                state.voice_service.set_music_service(state.music_service)
            logger.info("MusicService started")
        except Exception as e:
            logger.warning(f"MusicService failed to start: {e}")

    # Pre-render cached phrases in the background; lifecycle phrases first (they play right after boot).
    def _prerender_cached_phrases():
        if not state.tts_service or not getattr(state.tts_service, "available", False):
            return
        try:
            state.tts_service.warm_lifecycle_phrases()
        except Exception as e:
            logger.warning("Lifecycle phrase prerender failed: %s", e)
        try:
            from hal.routes.music import _backchannel_pool
            for phrase in _backchannel_pool():
                state.tts_service.speak_cached(phrase, prerender=True)
        except Exception as e:
            logger.warning("Music backchannel prerender failed: %s", e)
        # Warm the rate-limit notice and touch-gesture acks too.
        try:
            from hal.drivers.button_actions import _current_lang
            from hal.i18n import (
                DEFAULT_LANG,
                MIC_MUTED_PHRASES_BY_LANG,
                MIC_UNMUTED_PHRASES_BY_LANG,
            )

            lang = _current_lang()
            for pools in (MIC_MUTED_PHRASES_BY_LANG, MIC_UNMUTED_PHRASES_BY_LANG):
                for phrase in pools.get(lang) or pools.get(DEFAULT_LANG, []):
                    state.tts_service.speak_cached(phrase, prerender=True)
        except Exception as e:
            logger.warning("Gesture ack prerender failed: %s", e)
        try:
            from hal.i18n import PHRASE_RATE_LIMIT, localized_phrase

            notice = localized_phrase(PHRASE_RATE_LIMIT)
            if notice:
                state.tts_service.speak_cached(notice, prerender=True)
        except Exception as e:
            logger.warning("Rate-limit notice prerender failed: %s", e)

    threading.Thread(
        target=_prerender_cached_phrases,
        daemon=True,
        name="prerender-cached-phrases",
    ).start()

    # Phase 3: wait for hardware threads, then start dependent services.
    for t in hw_threads:
        t.join(timeout=10)

    sensing_enabled = os.environ.get("HAL_SENSING_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    # SensingService opens perception channels sequentially (~4-5s); run it in the background.
    def _start_sensing():
        try:
            # `presence` gates face identity + facial emotion models.
            _has_presence = "presence" in _profile.capabilities
            if _lifespan_stopping.is_set():
                logger.info("sensing-init: shutdown already in progress — skipping")
                return
            svc = SensingService(
                camera_capture=state.camera_capture,
                # Declared or absent, never guessed: None skips SoundPerception.
                input_device=AUDIO_SENSING_DEVICE,
                poll_interval=float(os.environ.get("HAL_SENSING_INTERVAL", "2.0")),
                rgb_service=state.rgb_service,
                tts_service=state.tts_service,
                animation_service=state.animation_service,
                is_sleeping=lambda: state._sleeping,
                enable_people_perception=_has_presence,
            )
            # Shutdown may have begun during the ~4-5s constructor; don't start it then.
            if _lifespan_stopping.is_set():
                logger.info("sensing-init: shutdown began during construction — not starting")
                return
            state.sensing_service = svc
            svc.start()
            logger.info("SensingService started (people_perception=%s via presence capability)", _has_presence)
        except Exception as e:
            logger.warning(f"SensingService failed to start: {e}")
            state.sensing_service = None

    if SensingService and sensing_enabled:
        threading.Thread(target=_start_sensing, daemon=True, name="sensing-init").start()

    # Warm the look-aim detector so its lazy model load never lands inside a look deadline.
    if LOOK_AIM_ENABLED and "camera" in _plan.mounted:
        def _warm_look_aim():
            try:
                from hal.drivers.tracking.aim import prewarm
                prewarm()
            except Exception as e:
                logger.debug("look-aim prewarm unavailable: %s", e)

        threading.Thread(target=_warm_look_aim, daemon=True, name="warm-look-aim").start()

        # Learn where the user usually sits, passively. Without this the bearing
        # only learns from perfectly-centred look questions and decays faster
        # than it accumulates.
        try:
            from hal.drivers.tracking import bearing_sampler

            bearing_sampler.start()
        except Exception as e:
            logger.debug("bearing sampler unavailable: %s", e)

        # Off by default; shadow-logs when on.
        try:
            from hal.drivers.tracking import gaze

            gaze.start()
        except Exception as e:
            logger.debug("gaze watcher unavailable: %s", e)

    if DisplayService:
        try:
            state.display_service = DisplayService()
            state.display_service.start()
            logger.info("DisplayService started")
        except Exception as e:
            logger.warning(f"DisplayService failed to start: {e}")
            state.display_service = None

    # Needs both a camera and a servo; tracker routes are None-tolerant.
    if "servo" in _plan.mounted and "camera" in _plan.mounted:
        from hal.drivers.tracking import TrackerService
        state.tracker_service = TrackerService()
        logger.info("TrackerService initialized")
    else:
        logger.info("TrackerService skipped — needs servo+camera routes mounted")

    # Each declared button owns its GPIO handle and gesture state.
    _gpio_button_handlers = []
    for button in _gpio_button_configs:
        try:
            from hal.drivers.gpio_button import GPIOButtonHandler

            handler = GPIOButtonHandler(
                button.wiring, name=button.name, behavior=button.behavior,
                hold_s=button.hold_s, factory_reset=button.factory_reset,
            )
            handler.start()
            _gpio_button_handlers.append(handler)
        except Exception as e:
            logger.warning("GPIO button %s init failed: %s", button.name, e)
    if not _gpio_button_configs:
        logger.info("GPIO button skipped — mock board has no hardware")

    # MPR121 is opt-in per device/board; absent hardware leaves other inputs running.
    if _mpr121_config is not None:
        try:
            from hal.drivers.mpr121 import MPR121Handler

            _mpr121_handler = MPR121Handler(_mpr121_config)
            _mpr121_handler.start()
        except Exception as e:
            logger.warning(
                "MPR121 init failed on i2c-%d address 0x%02x: %s",
                _mpr121_config.bus, _mpr121_config.address, e,
            )
            _mpr121_handler = None
    else:
        logger.info("MPR121 skipped — no enabled device wiring or simulated board")

    # OrangePi sun60 only; skips silently on other boards.
    try:
        from hal.drivers.ttp223 import TTP223Handler

        _ttp223_handler = TTP223Handler(_ttp223_config)
        _ttp223_handler.start()
    except Exception as e:
        logger.warning(f"TTP223 init failed: {e}")

    # Slide-switch position controls mic mute; missing config preserves legacy gating.
    try:
        from hal.drivers.privacy_button import PrivacyButtonHandler

        _privacy_button_handler = PrivacyButtonHandler(_privacy_button_config)
        _privacy_button_handler.start()
    except Exception as e:
        logger.warning(f"Mic switch init failed: {e}")

    # Best effort: falls back to the device speaker/mic.
    if "bluetooth" in _plan.mounted:
        try:
            from hal.drivers.audio_route import maybe_restore_bt_route
            threading.Thread(
                target=maybe_restore_bt_route, daemon=True, name="bt-route-restore"
            ).start()
        except Exception as e:
            logger.warning(f"BT route restore scheduling failed: {e}")

    # Re-apply the pre-restart scene (boot-scoped sidecar).
    try:
        from hal.routes.scene import restore_persisted_scene
        threading.Thread(
            target=restore_persisted_scene, daemon=True, name="scene-restore"
        ).start()
    except Exception as e:
        logger.warning(f"Scene restore scheduling failed: {e}")

    # Flag already set: _start_mic_muted_effect (not _apply_) paints the indicator.
    if state._mic_muted:
        try:
            state._start_mic_muted_effect()
        except Exception as e:
            logger.warning(f"Mic-muted LED repaint failed: {e}")

    # Do NOT re-express `sleepy` (it plays the animation); only restore emotion bookkeeping.
    if state._sleeping:
        try:
            from hal.presets import EMO_SLEEPY

            state._current_emotion = EMO_SLEEPY
            logger.info(
                "Sleep restored: asleep, mic_muted=%s speaker_muted=%s (no wake performance)",
                state._mic_muted, state._speaker_muted,
            )
        except Exception as e:
            logger.warning(f"Sleep restore bookkeeping failed: {e}")

    # Thermal fail-safe monitor (only when `thermal` bounds are declared).
    if _safety and _safety.thermal:
        threading.Thread(
            target=_thermal_monitor, args=(_safety,), daemon=True, name="thermal-monitor"
        ).start()
        logger.info(
            "Thermal monitor: max_temp_c=%d resume_temp_c=%d",
            _safety.thermal.max_temp_c, _safety.thermal.resume_temp_c,
        )

    if "environment" in _plan.mounted:
        state.environment_service = _environment_group
        state.environment_service.start()

    logger.info("[startup] lifespan_ready elapsed_ms=%.0f init_ms=%.0f",
                (time.perf_counter() - _startup_started) * 1000,
                (time.perf_counter() - _lifespan_started) * 1000)
    yield

    _lifespan_stopping.set()
    if state.environment_service is not None:
        state.environment_service.stop()
    _thermal_stop.set()
    if _privacy_button_handler is not None:
        _privacy_button_handler.stop()
    for handler in _gpio_button_handlers:
        handler.stop()
    _gpio_button_handlers = []
    if _mpr121_handler is not None:
        _mpr121_handler.stop()

    # Voice/sensing stops (~3s) run concurrently with the announce+park below.
    _shutdown_threads = []
    if state.voice_service:
        close_voice = getattr(state.voice_service, "close", state.voice_service.stop)
        _shutdown_threads.append(threading.Thread(target=close_voice, daemon=True))
    if state.sensing_service:
        _shutdown_threads.append(threading.Thread(target=state.sensing_service.stop, daemon=True))
    for t in _shutdown_threads:
        t.start()

    # Announce + park while tts_service is still alive.
    from hal.drivers.os_shutdown import announce_os_shutdown
    announce_os_shutdown()

    state._stop_current_effect()
    if state.display_service:
        state.display_service.stop()
    if state.music_service and state.music_service.playing:
        state.music_service.stop()

    if state.tracker_service and state.tracker_service.is_tracking:
        state.tracker_service.stop()

    for t in _shutdown_threads:
        # Best-effort: systemd kills the cgroup right after.
        t.join(timeout=3)

    if state.animation_service:
        # MotionService.stop() works on every backend; wrapped because shutdown must not raise.
        try:
            state.animation_service.stop(timeout=3.0)
        except Exception as e:
            logger.warning(f"Motion service stop failed: {e}")
    if state.rgb_service:
        state.rgb_service.stop()
    if state.camera_capture:
        state.camera_capture.stop()

    # Give the hardware back last, after every handle is closed.
    for _owner in _media_owners:
        _owner.acquire()


app = FastAPI(
    title="HAL Hardware Runtime",
    description=API_DESCRIPTION,
    version=(Path(__file__).parent / "VERSION_HAL").read_text().strip()
    if (Path(__file__).parent / "VERSION_HAL").exists()
    else "dev",
    lifespan=lifespan,
    # Custom /docs handler serves Swagger without inline <script> (CSP `script-src 'self'`).
    docs_url=None,
    redoc_url="/redoc",
    # `servers` tells Swagger UI which base URL to prepend on "Try it out".
    # In the browser context the iframe lives at /api/hardware/docs and admin
    # auth gates /api/hardware/* via the OS server's reverse proxy; in the loopback /
    # SSH-tunnel context calls go directly to HAL. Operator can switch
    # between them via the Swagger UI dropdown.
    servers=[
        {"url": "/api/hardware", "description": "Via OS server admin proxy (browser)"},
        {"url": "/", "description": "Direct (loopback / SSH tunnel)"},
    ],
    openapi_tags=OPENAPI_TAGS,
)

# Mount routes by crossing ROBOT.md declarations with driver availability (plan_mounts):
# declared+available -> mount; declared+required+missing -> fail loud; otherwise skip.

# Undeclared hardware route modules are never imported; driverless routes always load.
import importlib

_ALWAYS_ROUTES = ("audio", "emotion", "scene", "system", "bluetooth")
_ROUTERS_BY_NAME = {}
for _rname in (
    "servo", "led", "camera", "audio", "emotion", "scene", "sensing",
    "display", "voice", "music", "system", "bluetooth", "policy", "environment",
):
    if _rname not in _declared and _rname not in _ALWAYS_ROUTES:
        logger.info("Route module '%s' skipped — not declared in ROBOT.md", _rname)
        continue
    _ROUTERS_BY_NAME[_rname] = importlib.import_module(f"hal.routes.{_rname}").router

if "speaker" in _declared:
    try:
        from hal.routes.speaker import router as _speaker_router

        _ROUTERS_BY_NAME["speaker"] = _speaker_router
    except Exception as _speaker_import_err:  # noqa: BLE001
        logger.warning("Speaker recognition router unavailable: %s", _speaker_import_err)


# Availability = driver code importable; connection faults surface later in lifespan().
_route_available = {
    "servo": AnimationService is not None,
    "led": RGBService is not None,
    "camera": cv2 is not None and LocalVideoCaptureDevice is not None and VideoCaptureDeviceInfo is not None,
    "audio": sd is not None,
    "voice": VoiceService is not None,
    "sensing": SensingService is not None,
    "display": DisplayService is not None,
    "music": MusicService is not None,
    # Logging-only policy route: no model or actuator dependency.
    "policy": True,
    "environment": True,
    "emotion": True, "scene": True, "system": True, "bluetooth": True,
    "speaker": "speaker" in _ROUTERS_BY_NAME,
}

# SAFETY.md bounds resolved once at boot; absent = pass-through, malformed = fail loud.
_device_dir = os.path.join(_devices_dir(), _resolve_device_type())
_safety = _boot.safety

# Unreadable or absent geometry is a warning and pass-through, never a boot failure.
from hal.drivers.motors.recording_stability import load_geometry
_geometry = load_geometry(_device_dir, _profile.urdf_ref)

if "policy" in _declared:
    from hal.policy.service import LoggingPolicyService

    state.policy_service = LoggingPolicyService(logger)
state.safety_policy = _safety  # route-level gates (e.g. music quiet hours) read it here

# Merge presets.json onto base tables at import, before any route reads a preset.
_led_count = _boot.led_count
logger.info(
    "Safety policy: device=%s max_brightness=%s light_quiet=%s audio_quiet=%s",
    _resolve_device_type(),
    _safety.max_brightness if _safety else None,
    bool(_safety and _safety.light_quiet),
    bool(_safety and _safety.audio_quiet),
)


def _safety_view(p):
    """Serialize the resolved SafetyPolicy for GET /device (null when no bounds)."""
    if p is None:
        return None

    def _qh(q):
        if q is None:
            return None
        d = {"start": q.start.strftime("%H:%M"), "end": q.end.strftime("%H:%M")}
        if q.max_brightness is not None:
            d["max_brightness"] = q.max_brightness
        return d

    light = {}
    if p.max_brightness is not None:
        light["max_brightness"] = p.max_brightness
    if p.light_quiet is not None:
        light["quiet_hours"] = _qh(p.light_quiet)
    out = {}
    if light:
        out["light"] = light
    if p.audio_quiet is not None:
        out["audio"] = {"quiet_hours": _qh(p.audio_quiet)}
    if p.motion is not None:
        m = {"stop_always": p.motion.stop_always}
        if p.motion.max_speed is not None:
            m["max_speed"] = p.motion.max_speed
        out["motion"] = m
    if p.thermal is not None:
        out["thermal"] = {
            "max_temp_c": p.thermal.max_temp_c,
            "resume_temp_c": p.thermal.resume_temp_c,
        }
    return out or None


# Thermal monitor only when `thermal` bounds are declared; stops tracking, not idle.
_thermal_stop = threading.Event()


def _thermal_monitor(policy, interval: float = 10.0):
    from hal.safety.policy import read_soc_temp_c, thermal_over
    while not _thermal_stop.is_set():
        temp = read_soc_temp_c()
        state.soc_temp_c = temp
        over = thermal_over(policy, temp, state.thermal_over)
        if over and not state.thermal_over:
            state.thermal_over = True
            logger.warning(
                "[thermal] SoC %.1f°C >= %d°C — over-temp; stopping discretionary motion",
                temp, policy.thermal.max_temp_c,
            )
            try:
                if state.tracker_service and state.tracker_service.is_tracking:
                    state.tracker_service.stop()
            except Exception as e:
                logger.warning("[thermal] stop tracking failed: %s", e)
        elif state.thermal_over and not over:
            state.thermal_over = False
            logger.info(
                "[thermal] SoC %s°C <= %d°C — recovered",
                f"{temp:.1f}" if temp is not None else "?", policy.thermal.resume_temp_c,
            )
        _thermal_stop.wait(interval)


def _thermal_view():
    """Thermal status for GET /health — null when no `thermal` bound is declared."""
    if not (_safety and _safety.thermal):
        return None
    return {
        "over": state.thermal_over,
        "temp_c": state.soc_temp_c,
        "max_temp_c": _safety.thermal.max_temp_c,
    }

# Board gate: fail loud on a board not declared in ROBOT.md `boards` (raw match).
# Simulation and HAL_BOARD=host never initialize local GPIO.
_board_id = _boot.board
logger.info("Board gate: device=%s board=%s declared=%s", _resolve_device_type(), _board_id, _profile.boards)

from hal.board.gpio_button import load_button_configs

_gpio_button_configs = (
    [] if _board_id in {"sim", "host"} else load_button_configs(_device_dir, _board_id)
)

from hal.board.privacy_button import load_privacy_button_config

_privacy_button_config = (
    None if _board_id in {"sim", "host"} else load_privacy_button_config(
        _device_dir, _board_id, _resolve_device_type(),
    )
)

from hal.board.mpr121 import load_mpr121_config

_mpr121_config = (
    None if _board_id in {"sim", "host"} else load_mpr121_config(_device_dir, _board_id)
)

_environment_group = None
if "environment" in _declared:
    from hal.drivers.environment.group import create_environment_group

    _environment_group = create_environment_group(_device_dir, _board_id, _simulation)
    logger.info("[environment] components=%s simulation=%s", list(_environment_group.components), _simulation)
else:
    logger.info("[environment] capability not declared; acquisition disabled")

from hal.board.ttp223 import load_touch_config

_ttp223_config = (
    None if _board_id in {"sim", "host"} else load_touch_config(_device_dir, _board_id)
)

from hal.board.device import plan_mounts

_available = {r: _route_available.get(r, False) for r in _declared}
_plan = plan_mounts(_declared, _available)
logger.info(
    "Declaration-driven mount plan: device=%s mounted=%s skipped=%s failed_required=%s",
    _resolve_device_type(), _plan.mounted, _plan.skipped, _plan.failed_required,
)
# A required route without an importable driver aborts boot in every mode.
if not _plan.ok:
    raise RuntimeError(
        f"Device '{_resolve_device_type()}' requires routes whose drivers are "
        f"unavailable: {_plan.failed_required}. Fix the driver/hardware, or mark "
        f"the capability optional in robots/{_resolve_device_type()}/ROBOT.md."
    )
for _name in _plan.mounted:
    app.include_router(_ROUTERS_BY_NAME[_name])

# Self-hosted Swagger assets (no CDN) for the strict CSP.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    logger.warning("Swagger UI static dir missing: %s", _STATIC_DIR)


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Serve Swagger UI without inline <script> so the CSP can stay `script-src 'self'`."""
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{app.title} - Swagger UI</title>\n"
        '  <link rel="stylesheet" href="./static/swagger-ui.css">\n'
        "</head>\n"
        "<body>\n"
        '  <div id="swagger-ui"></div>\n'
        '  <script src="./static/swagger-ui-bundle.js"></script>\n'
        '  <script src="./static/swagger-init.js"></script>\n'
        "</body>\n"
        "</html>\n"
    )
    return HTMLResponse(content=html)


@app.get("/simulator", include_in_schema=False)
def simulator() -> HTMLResponse:
    """Local Lamp visualizer, available only when HAL is in simulation mode."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Simulation mode is not enabled")
    page = _STATIC_DIR / "lamp-simulator.html"
    if not page.is_file():
        return HTMLResponse(status_code=500, content="Lamp simulator assets are missing")
    return HTMLResponse(content=page.read_text(encoding="utf-8"))


@app.get("/simulator/reference", include_in_schema=False)
def simulator_reference():
    """Serve the checked-in physical Lamp reference image to the laptop UI."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Lamp simulator is unavailable")
    image = Path(_devices_dir()) / "lamp" / "images" / "lamp-white.webp"
    if not image.is_file():
        return HTMLResponse(status_code=404, content="Lamp reference image is unavailable")
    return FileResponse(image, media_type="image/webp")


@app.get("/simulator/cad", include_in_schema=False)
def simulator_cad():
    """Serve the Lamp's static CAD mesh (STL) for the local viewer."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Lamp simulator is unavailable")
    mesh = Path(_devices_dir()) / "lamp" / "hardware" / "cad" / "stl" / "lamp.stl"
    if not mesh.is_file() or mesh.stat().st_size < 1024:
        return HTMLResponse(
            status_code=404,
            content="Lamp CAD mesh is unavailable; run git lfs pull in the repository.",
        )
    return FileResponse(mesh, media_type="model/stl")


@app.get("/simulator/pixels", include_in_schema=False)
def simulator_pixels():
    """Every pixel on the ring right now, read from the strip buffer, for the local viewer."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Lamp simulator is unavailable")
    service = state.rgb_service
    if not service:
        return {"pixels": []}
    strip = service.strip
    pixels = []
    for index in range(service.led_count):
        raw = strip.getPixelColor(index)
        # Real strips pack a pixel into an int; the in-memory strip keeps tuples.
        pixels.append(
            list(raw)[:3] if isinstance(raw, (tuple, list))
            else [(raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF]
        )
    return {"pixels": pixels}


@app.get("/simulator/rig", include_in_schema=False)
def simulator_rig():
    """Serve the Lamp's rigged GLB (joint nodes named like HAL joints) for the local viewer."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Lamp simulator is unavailable")
    model = Path(_devices_dir()) / "lamp" / "hardware" / "cad" / "glb" / "lamp.glb"
    if not model.is_file() or model.stat().st_size < 1024:
        return HTMLResponse(
            status_code=404,
            content="Lamp rig is unavailable; run git lfs pull in the repository.",
        )
    return FileResponse(model, media_type="model/gltf-binary")


@app.get("/simulator/state", include_in_schema=False)
def simulator_state():
    """Expose the local UI mode and the rig's zero pose (the center preset); changes no state."""
    if not _simulation or _profile.id != "lamp":
        return HTMLResponse(status_code=404, content="Lamp simulator is unavailable")
    from hal.presets import AIM_CENTER, AIM_PRESETS

    # `media` is the effective mode; `media_camera` / `media_audio` / `media_reasons` give details.
    effective = (
        "host"
        if state.sim_media_camera == "host" and state.sim_media_audio == "host"
        else "virtual"
    )
    return {
        "media": effective,
        "media_requested": _simulation_media,
        "media_camera": state.sim_media_camera,
        "media_audio": state.sim_media_audio,
        "media_reasons": dict(state.sim_media_reasons),
        "rig_zero": AIM_PRESETS[AIM_CENTER],
    }


from hal.server_support.http_security import (
    ProxyPrefixMiddleware,
    local_only_middleware,
    request_logging_middleware,
)

# Order matters: ProxyPrefix, then the access gate, then request logging.
app.add_middleware(ProxyPrefixMiddleware)
app.middleware("http")(local_only_middleware)
app.middleware("http")(request_logging_middleware)


@app.get("/version", tags=["System"])
def version():
    """Return HAL runtime version."""
    return {"version": app.version}


@app.get("/device", tags=["System"])
def device():
    """This device's identity from ROBOT.md plus the resolved board and mounted routes."""
    return {
        "id": _profile.id,
        "name": _profile.name,
        "type": _profile.type,
        "schema": _profile.schema,
        "board": _board_id,
        # Resolved board wiring, not transient driver health or device type.
        "inputs": {"mpr121": _mpr121_config is not None},
        "boards": _profile.boards,
        "safety_ref": _profile.safety_ref,
        # Enforced safety bounds; null when none are declared.
        "safety": _safety_view(_safety),
        "memory": {"backend": _profile.memory_backend} if _profile.memory_backend else None,
        "routes": sorted(_plan.mounted),
        "drivers": {g: c.driver for g, c in _profile.capabilities.items() if c.driver},
    }


@app.get("/health", response_model=HealthResponse, tags=["System"])
def health():
    """Check which hardware drivers are available."""
    return {
        "status": "ok",
        "servo": state.animation_service is not None and state.animation_service.is_connected,
        "led": state.rgb_service is not None and state.rgb_service._driver is not None,
        "camera": state.camera_capture is not None and state.camera_capture.last_frame is not None,
        "audio": state.audio_output_device is not None or state.audio_input_device is not None,
        "sensing": state.sensing_service is not None,
        "environment": (
            state.environment_service is not None
            and not state.environment_service.snapshot()["stale"]
        ),
        "voice": state.voice_service is not None and state.voice_service.available
        if state.voice_service
        else False,
        "tts": state.tts_service is not None and state.tts_service.available
        if state.tts_service
        else False,
        "music": state.music_service is not None and state.music_service.available
        if state.music_service
        else False,
        "display": state.display_service is not None,
        "thermal": _thermal_view(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
