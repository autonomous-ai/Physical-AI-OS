"""Realtime agent turn handling — extracted from VoiceService._stream_session."""

import logging
import re
import threading
import time
from typing import Callable, NamedTuple, Optional

import requests

from hal import app_state as hal_app_state
from hal import config as hal_config
from hal import presets
from hal.clock import device_now
from hal.realtime.config import gemini_needs_idle_workaround
from hal.realtime.voice_agent.base import AudioTurnSessionChanged
from hal.realtime.models import AudioOutput as RTAudioOutput
from hal.realtime.models import TextOutput as RTTextOutput
from hal.realtime.models.signal import DelegateSignal, LookReplaySignal, RejectSignal
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice.tts.gemini import native_voice
from hal.drivers.voice._internal.cot_leak_filter import CoTLeakFilter, clean_transcript

logger = logging.getLogger("hal.voice")

SENTENCE_ENDS = (".", "!", "?", "。", "！", "？")

CLAUSE_ENDS = (",", ";", ":", "—", "，", "；", "：", "、")
# Below this many characters a clause is a runt ("Ừ,", "Sure,", "Vâng,") — speaking it
# alone sounds like a stutter, and the pause it buys is not worth it.
FIRST_CHUNK_MIN_CHARS = 8


def split_delivery_sentence(buf):
    """Hold the last sentence until following words or EOF attach late tags.

    ElevenLabs rejects tag-only requests. A trailing reaction arriving in the
    next model chunk must stay with the sentence rather than become a new call.
    """
    head, tail = split_completed_prefix(buf, first=True)
    if not head:
        return "", buf
    # Scan balanced brackets: HW JSON can itself contain arrays.
    offset = 0
    reaction_end = 0
    prefix_only = True
    while True:
        while offset < len(tail) and tail[offset].isspace():
            offset += 1
        if prefix_only:
            reaction_end = offset
        if offset == len(tail):
            return "", buf
        if tail[offset] != "[":
            break
        start, depth = offset, 0
        while offset < len(tail):
            char = tail[offset]
            offset += 1
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if not depth:
                    break
        if depth:
            return "", buf
        tag = tail[start + 1:offset - 1].strip().lower()
        reaction = re.fullmatch(
            r"(?:laughs?|laughing|giggles?|giggle|chuckles?|sobs?|sobbing|crying|"
            r"sighs?|gasps?|pause|short pause|long pause|big laugh|hearty laugh)", tag,
        )
        prefix_only = prefix_only and bool(reaction)
        if prefix_only:
            reaction_end = offset
    return head + tail[:reaction_end], tail[reaction_end:]


def split_realtime_first_chunk(buf, tts, strip_markers):
    """Keep fast clause streaming unless a sentence is waiting for late tags."""
    if getattr(tts, "_provider", None) == "elevenlabs":
        visible = realtime_visible_text(buf, tts, strip_markers)
        ready, _ = split_completed_prefix(buf)
        if ready or visible.rstrip().endswith(SENTENCE_ENDS):
            return "", buf
    return split_first_chunk(buf)


def realtime_visible_text(text, tts, strip_markers):
    """Check sentence boundaries without counting complete delivery tags."""
    text = strip_markers(text)
    if getattr(tts, "_provider", None) == "elevenlabs":
        text = re.sub(r"\[[^\]]*\]", "", text).strip()
    return text


def realtime_speech_text(text, tts, strip_markers):
    """Keep delivery cues only on the ElevenLabs synthesis path."""
    if getattr(tts, "_provider", None) == "elevenlabs":
        return re.sub(r"\[[^\]]*$", "", strip_markers(text, preserve_audio_tags=True)).strip()
    return strip_markers(text)


def split_first_chunk(buf: str) -> tuple[str, str]:
    """Split the turn's FIRST utterance into (speak_now, keep_buffering)."""
    cap: int = hal_config.REALTIME_FIRST_CHUNK_MAX_CHARS
    if cap <= 0 or len(buf) < FIRST_CHUNK_MIN_CHARS or buf.rstrip().endswith(SENTENCE_ENDS):
        return "", buf
    # Network text can end inside a voice tag or a number. Only consider
    # boundaries outside square brackets, and never cut after a digit.
    clauses = []
    spaces = []
    depth = 0
    visible = 0
    for i, char in enumerate(buf):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif not depth:
            visible += 1
            if visible < FIRST_CHUNK_MIN_CHARS:
                continue
            if char in CLAUSE_ENDS and not (i and buf[i - 1].isdigit()):
                if char not in (":", "：") or (i + 1 < len(buf) and buf[i + 1].isspace()):
                    clauses.append(i)
            if char.isspace() and i < cap:
                spaces.append(i)
    cut = clauses[-1] if clauses else -1
    if cut < 0:
        if len(buf) < cap or not spaces:
            return "", buf
        cut = spaces[-1]
    head: str = buf[: cut + 1].strip()
    return (head, buf[cut + 1:]) if head else ("", buf)


def split_completed_prefix(buf: str, *, first: bool = False) -> tuple[str, str]:
    """Release completed sentences bundled with the next unfinished sentence."""
    depth = 0
    cut = 0
    abbreviations = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e"}
    for i, char in enumerate(buf):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif not depth and char in SENTENCE_ENDS:
            if i + 1 >= len(buf) or not buf[i + 1].isspace():
                continue
            if char == ".":
                token = buf[:i].rsplit(maxsplit=1)[-1].lower() if buf[:i].strip() else ""
                if (i and buf[i - 1].isdigit()) or token in abbreviations or len(token) == 1:
                    continue
            if not first or not cut:
                cut = i + 1
    if depth or not cut or not buf[cut:].strip():
        return "", buf
    return buf[:cut], buf[cut:]


ROUTE_HANDLED = "realtime_handled"
ROUTE_DELEGATED = "delegated"
ROUTE_AI_REJECTED = "ai_rejected"
ROUTE_NO_OUTPUT = "realtime_no_output"
ROUTE_ERROR = "realtime_error"
ROUTE_UNAVAILABLE = "realtime_unavailable"
ROUTE_NOISE_DROPPED = "noise_dropped"       # never committed — noise guard rejected it
ROUTE_FOREIGN_DROPPED = "foreign_dropped"
ROUTE_NOT_STARTED = "realtime_not_started"


def _reply_language_name() -> str:
    """Resolve the device's reply language to a human name (e.g. "Vietnamese").

    Returns "" when no language is configured — we don't force a default, mirroring the
    prompt's `_load_language() or "English"` only when one is actually set.
    """
    from hal.config import _os_cfg_get
    from hal.realtime.context_manager.base import ContextManagerBase

    code: str = (_os_cfg_get("stt_language", "") or "").strip()
    if not code:
        return ""
    return ContextManagerBase.LANGUAGE_NAMES.get(code, code)


def _thinking_cue_start() -> None:
    """Show `thinking` while the realtime model works on the committed turn."""
    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=presets.EMO_THINKING))
        hal_app_state._thinking_cue_active = True
        hal_app_state._apply_emotion_led_display(
            presets.EMO_THINKING, 0.7, force_led=True
        )
    except Exception as e:
        logger.warning("[realtime] thinking cue failed: %s", e)


class _WaitFiller:
    """Speak one dead-air filler if the realtime model keeps the user waiting.

    The phrase fires only after REALTIME_FILLER_DELAY_S with still no output, so a reply
    that starts sooner is never interrupted by one.
    """

    def __init__(self, owner: str = "") -> None:
        self._timer: Optional[threading.Timer] = None
        self._fired = False
        # Voice metrics ownership: which utterance this "one moment" is for.
        self._owner = owner

    def arm(self) -> None:
        delay = hal_config.REALTIME_FILLER_DELAY_S
        if delay <= 0 or self._timer is not None or self._fired:
            return
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self._fired = True
        try:
            requests.post(
                voice_cfg.OS_FILLER_URL,
                json={"owner": self._owner} if self._owner else None,
                timeout=2,
            )
            logger.info(
                "[realtime] dead-air filler requested after %.1fs of no output",
                hal_config.REALTIME_FILLER_DELAY_S,
            )
        except Exception as e:
            logger.warning("[realtime] dead-air filler request failed: %s", e)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    @property
    def fired(self) -> bool:
        return self._fired


def _thinking_cue_clear() -> None:
    """Return to idle when the reply starts (or the turn produced nothing) — ONLY if the
    face still shows our `thinking`, so an emotion the model expressed via the
    express_emotion tool is never stomped.
    """
    try:
        # Drop the cue's claim on the strip first: every exit path calls this,
        # so the flag can never outlive the turn even when the guard below
        # returns early (model expressed its own emotion mid-turn).
        hal_app_state._thinking_cue_active = False
        if hal_app_state._current_emotion != presets.EMO_THINKING:
            return
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=presets.EMO_IDLE))
    except Exception as e:
        logger.warning("[realtime] thinking cue clear failed: %s", e)


def build_turn_context(speaker: Optional[str] = None) -> str:
    """Build per-turn context for the realtime model.

    The caller must send this before streaming audio for the turn.
    """
    turn_ctx: list[str] = [
        f"Time: {device_now().strftime('%Y-%m-%d %H:%M:%S %A')}",
    ]
    # Per-turn language reminder. The system prompt already locks the language, but
    # Google Search grounding pulls English source text into context and can drag the
    # spoken reply into English.
    lang_name: str = _reply_language_name()
    if lang_name:
        turn_ctx.append(
            f"Reply language: {lang_name} (answer ONLY in {lang_name}, "
            "even if a search result or any context is in another language)"
        )
    if speaker:
        turn_ctx.append(
            f"Current user: {speaker} (identified by voice — address this "
            f"person, not anyone else)"
        )
    else:
        try:
            if hal_app_state.sensing_service:
                cu: str = (
                    hal_app_state.sensing_service._perception_orchestrator.current_user
                    or ""
                )
                if cu:
                    turn_ctx.append(f"Current user: {cu}")
        except Exception:
            pass
    return "[TURN CONTEXT] " + " | ".join(turn_ctx)


def build_speaker_correction(speaker: str) -> str:
    """Late identity correction for a turn whose context was already sent."""
    return (
        f"[TURN CONTEXT UPDATE] Correction for THIS turn: the person speaking is "
        f"{speaker} (identified by voice). Address {speaker} — ignore any other "
        f"name for the current user in this turn's context or in your memory."
    )


class RealtimeTurnResult(NamedTuple):
    """Outcome of a realtime turn, consumed by the OS-server dispatch step."""

    delegated: bool = False
    handled: bool = False
    transcript: str = ""
    delegate_msg: str = ""
    route: str = ROUTE_NOT_STARTED
    # This is deliberately separate from `route`: an empty/no-output turn must
    # retain main-agent fallback. Only an explicit reject_turn tool call sets it.
    rejected: bool = False
    execution_completed: bool = False
    handoff_context: str = ""


def should_drop_realtime_rejection(rt: RealtimeTurnResult) -> bool:
    """Return whether the explicit AI rejection may suppress downstream dispatch."""
    return hal_config.REALTIME_AI_REJECT_FILTER and rt.rejected


def should_drop_downstream_turn(rt: RealtimeTurnResult) -> bool:
    """Return whether a terminal guard/model decision must stop OS dispatch."""
    return (
        rt.route in (ROUTE_NOISE_DROPPED, ROUTE_FOREIGN_DROPPED)
        or should_drop_realtime_rejection(rt)
    )


def should_dispatch_to_main(
    wakeword_enabled: bool,
    wakeword_authorized: bool,
) -> bool:
    """Return whether the finalized STT turn must reach the main agent."""
    if not wakeword_enabled:
        return True
    return wakeword_authorized


def needs_noise_guard(combined: str) -> bool:
    """Return whether the Silero voiced-ratio guard must run for this transcript."""
    if not combined:
        return True
    max_words: int = hal_config.REALTIME_NOISE_GUARD_MAX_WORDS
    return 0 < len(combined.split()) <= max_words


def should_arm_realtime_wait_filler(combined: str) -> bool:
    """Return whether this realtime turn may receive an audible wait filler."""
    return not needs_noise_guard(combined)


def should_defer_speaker_id_prepass(combined: str) -> bool:
    """Return whether speaker ID can wait for the explicit AI rejection verdict."""
    return (
        hal_config.REALTIME_ENABLED
        and hal_config.REALTIME_AI_REJECT_FILTER
        and bool(combined)
        and needs_noise_guard(combined)
    )


def is_nonactionable_transcript(combined: str) -> bool:
    """Return whether the whole transcript is only backchannel/filler words."""
    fillers = hal_config.REALTIME_NONACTIONABLE_FILLERS
    if not combined or not fillers:
        return False
    norm: str = re.sub(r"[^\w\s-]", " ", combined.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return False
    if norm in fillers:
        return True
    return all(tok.strip("-") in fillers for tok in norm.split())


# Scripts an English (or any non-CJK/non-Vietnamese) device must never speak.
_CJK_KANA_HANGUL = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_VIET_CHARS = re.compile(r"[đĐăĂâÂêÊôÔơƠưƯẠ-ỿ]")


def reply_is_foreign_script(text: str, reply_language: str) -> bool:
    """Return whether a realtime reply is in a script the device must not speak."""
    if not text or not hal_config.REALTIME_FOREIGN_SCRIPT_GUARD:
        return False
    lang: str = (reply_language or "English").strip().lower()
    if not lang.startswith(("chin", "japan", "korea", "zh", "ja", "ko")):
        if _CJK_KANA_HANGUL.search(text):
            return True
    if not lang.startswith(("viet", "vi")):
        if _VIET_CHARS.search(text):
            return True
    return False


def is_noise_turn(
    combined: str, buf_duration: float, audio_is_speech: bool = True
) -> bool:
    """Return whether this capture must not be committed to the realtime model."""
    if is_nonactionable_transcript(combined):
        return True
    # A short transcript that Silero judged non-speech is an STT fabrication over noise,
    # not a short command — drop it whatever REQUIRE_TRANSCRIPT says.
    if not audio_is_speech and combined and needs_noise_guard(combined):
        return True
    if hal_config.REALTIME_REQUIRE_TRANSCRIPT:
        return not combined
    return not combined and (
        buf_duration < hal_config.REALTIME_MIN_COMMIT_DURATION_S or not audio_is_speech
    )


def harness_followup_active() -> bool:
    """Return whether OS has a recent paired Harness exchange awaiting speech."""
    try:
        response = requests.get(voice_cfg.OS_HARNESS_FOLLOWUP_URL, timeout=0.15)
        return response.ok and response.json().get("data", {}).get("active") is True
    except (requests.RequestException, ValueError):
        return False


def _commit_turn_output(realtime, audio_frames, audio_turn=None):
    """Commit one session; replay a lost upload before any response is consumed."""
    if audio_turn is None:
        realtime.flush_output()
        realtime.commit_audio()
        logger.info("[realtime] Audio committed — streaming output")
        return None, realtime.stream_output()
    try:
        realtime.flush_output(turn=audio_turn)
        realtime.commit_audio(turn=audio_turn)
    except AudioTurnSessionChanged:
        if not realtime.recover_session("audio-upload-session-changed"):
            raise
        audio_turn = realtime.bind_audio_turn()
        realtime.send_text(build_turn_context())
        for frame in audio_frames:
            realtime.append_audio(frame, turn=audio_turn)
        realtime.flush_output(turn=audio_turn)
        realtime.commit_audio(turn=audio_turn)
    # A change after commit may have executed tools even without spoken output.
    # Let the existing error/fallback path handle it; never blindly repeat tools.
    logger.info("[realtime] Audio committed — streaming output")
    return audio_turn, realtime.stream_output(turn=audio_turn)


def run_realtime_turn(
    realtime,
    tts,
    strip_markers: Callable[[str], str],
    combined: str,
    rt_audio_buffer: list,
    buf_duration: float,
    audio_is_speech: bool = True,
    interaction_id: str = "",
    wait_filler: "Optional[_WaitFiller]" = None,
    save_history: bool = True,
    audio_turn=None,
    harness_followup: Optional[bool] = None,
) -> RealtimeTurnResult:
    """Commit the captured audio to the realtime agent and stream its reply."""
    delegated = False
    handled = False
    execution_completed = False
    rejected = False
    foreign_suppressed = False
    transcript = ""
    delegate_msg = ""
    handoff_context = ""
    route = ROUTE_NOT_STARTED
    native = (hal_config.REALTIME_NATIVE_AUDIO or native_voice(tts) is not None) and tts is not None
    native_started = False
    native_played = False

    if combined and (harness_followup if harness_followup is not None else harness_followup_active()):
        logger.info("[realtime] Harness follow-up active — delegating without realtime reply")
        return RealtimeTurnResult(delegated=True, delegate_msg=combined, route=ROUTE_DELEGATED)

    # Noise/false-trigger guard: a session with no STT transcript is not worth a model
    # turn. "No transcript" → don't speak.
    noise_turn = is_noise_turn(combined, buf_duration, audio_is_speech)
    if (
        hal_config.REALTIME_ENABLED
        and realtime.available
        and rt_audio_buffer
        and not noise_turn
    ):
        logger.info(
            "[realtime] Entering realtime flow — committing audio (stt=%r)",
            combined[:100] if combined else "(empty)",
        )
        thinking_started = False
        if wait_filler is None:
            wait_filler = _WaitFiller(owner=interaction_id)
        if should_arm_realtime_wait_filler(combined):
            wait_filler.arm()
        else:
            logger.info(
                "[realtime] Short transcript — suppressing dead-air filler while "
                "the model decides whether to reject"
            )
        try:
            # 1011 recovery (idle-death): the campaign-api proxy drops idle
            # 2.5-native-audio sessions, so a turn that follows a pause lands on a dead
            # session and Gemini returns WS 1011 with NO output. See
            # gemini_needs_idle_workaround.
            is_gemini: bool = hal_config.REALTIME_PROVIDER.strip().lower() == "gemini"
            max_retries: int = (
                hal_config.REALTIME_GEMINI_TURN_RETRIES
                if (is_gemini and gemini_needs_idle_workaround())
                else 0
            )
            text_parts: list[str] = []
            sentence_buf: str = ""
            first_sentence_sent: bool = False
            attempt: int = 0
            look_replayed: bool = False
            reply_lang: str = _reply_language_name()
            leak_filter = CoTLeakFilter(reply_lang)
            while True:
                if attempt > 0:
                    logger.info(
                        "[realtime] No output (likely WS 1011) — fresh session + "
                        "replay audio (retry %d/%d)",
                        attempt,
                        max_retries,
                    )
                    if not realtime.recover_session("gemini-1011-replay"):
                        break
                    if audio_turn is not None:
                        audio_turn = realtime.bind_audio_turn()
                    for _frame in rt_audio_buffer:
                        realtime.append_audio(_frame, **({"turn": audio_turn} if audio_turn is not None else {}))
                    text_parts = []
                    sentence_buf = ""
                    first_sentence_sent = False
                    leak_filter = CoTLeakFilter(reply_lang)

                t_commit: float = time.monotonic()
                first_output_logged: bool = False

                execution_completed = False
                look_replay: bool = False
                audio_turn, outputs = _commit_turn_output(realtime, rt_audio_buffer, audio_turn)
                if not thinking_started:
                    # Start provider work before synchronous hardware feedback.
                    thinking_started = True
                    _thinking_cue_start()
                native_pending = []
                native_pending_samples = 0
                for output in outputs:
                    if not first_output_logged:
                        first_output_logged = True
                        logger.info(
                            "[realtime] first output +%.2fs after commit (%s)",
                            time.monotonic() - t_commit,
                            type(output).__name__,
                        )
                    if isinstance(output, LookReplaySignal):
                        look_replay = True
                        continue
                    if isinstance(output, DelegateSignal):
                        delegated = True
                        delegate_msg = output.message
                        handoff_context = output.handoff_context
                        # The wait is over — the main-agent hop that follows has
                        # its own filler (os-server fires one on the forwarded
                        # voice turn), so ours must not fire on top of it.
                        wait_filler.cancel()
                        continue
                    if isinstance(output, RejectSignal):
                        rejected = True
                        wait_filler.cancel()
                        break
                    if delegated:
                        continue
                    if native and isinstance(output, RTAudioOutput):
                        if not native_started:
                            native_pending.append(output.audio)
                            native_pending_samples += len(output.audio)
                            if native_pending_samples > realtime.output_sample_rate * 30:
                                logger.warning("[realtime] Native speaker unavailable for 30s of audio — abandoning reply")
                                break
                            wait_filler.cancel()
                            native_started = tts.native_play_begin(
                                realtime.output_sample_rate,
                                owner=f"interaction:{interaction_id}" if interaction_id else "",
                            )
                            if native_started:
                                logger.info(
                                    "[realtime] Native audio → playing model voice "
                                    "(+%.2fs after commit)",
                                    time.monotonic() - t_commit,
                                )
                                _thinking_cue_clear()
                                wait_filler.cancel()
                        if native_started:
                            if native_pending:
                                for frame in native_pending:
                                    if not tts.native_play_frame(frame):
                                        break
                                native_pending.clear()
                                native_pending_samples = 0
                            else:
                                tts.native_play_frame(output.audio)
                        if output.transcript:
                            text_parts.append(output.transcript)
                        continue
                    if isinstance(output, RTTextOutput):
                        if not text_parts and output.text:
                            logger.info("[tts-timing] stage=realtime_first_text mode=turn owner=%s", interaction_id)
                        text_parts.append(output.text)
                        if native:
                            # Audio already carries the reply — keep text only for
                            # memory + the [HANDLED] hint; don't synthesize it.
                            continue
                        sentence_buf += output.text
                        # Wrong-script reply (e.g. noise hallucinated as JP/KR, or a
                        # Vietnamese reply on an English device): never speak it.
                        if not foreign_suppressed and reply_is_foreign_script(
                            "".join(text_parts), reply_lang
                        ):
                            foreign_suppressed = True
                            logger.info(
                                "[realtime] Foreign-script reply suppressed (lang=%s): %r",
                                reply_lang, "".join(text_parts)[:60],
                            )
                            wait_filler.cancel()
                            _thinking_cue_clear()
                        if foreign_suppressed:
                            sentence_buf = ""
                            continue
                        if tts is not None and not first_sentence_sent:
                            head, rest = split_realtime_first_chunk(sentence_buf, tts, strip_markers)
                            head = leak_filter.filter_text(realtime_speech_text(head, tts, strip_markers)) if head else ""
                            if head:
                                logger.info(
                                    "[realtime] First clause → speak (+%.2fs after "
                                    "commit): %r",
                                    time.monotonic() - t_commit,
                                    head[:80],
                                )
                                wait_filler.cancel()
                                if not tts.speak(head, turn_id=interaction_id, realtime_reply=True):
                                    tts.speak_queue(head, turn_id=interaction_id, realtime_reply=True)
                                first_sentence_sent = True
                                _thinking_cue_clear()
                                sentence_buf = rest
                        sentence = realtime_visible_text(sentence_buf, tts, strip_markers)
                        complete = sentence.rstrip().endswith(SENTENCE_ENDS)
                        ready, tail = ("", "") if complete else split_completed_prefix(sentence_buf)
                        if getattr(tts, "_provider", None) == "elevenlabs":
                            ready, tail = split_delivery_sentence(sentence_buf)
                            sentence = realtime_visible_text(ready, tts, strip_markers)
                        elif ready:
                            sentence = realtime_visible_text(ready, tts, strip_markers)
                        if tts is not None and sentence.rstrip().endswith(SENTENCE_ENDS):
                            sentence = leak_filter.filter_text(realtime_speech_text(ready if ready else sentence_buf, tts, strip_markers))
                            if sentence:
                                if not first_sentence_sent:
                                    logger.info(
                                        "[realtime] First sentence → speak (+%.2fs "
                                        "after commit): %r",
                                        time.monotonic() - t_commit,
                                        sentence[:80],
                                    )
                                    wait_filler.cancel()
                                    if not tts.speak(sentence, turn_id=interaction_id, realtime_reply=True):
                                        tts.speak_queue(sentence, turn_id=interaction_id, realtime_reply=True)
                                    first_sentence_sent = True
                                    _thinking_cue_clear()
                                else:
                                    logger.info(
                                        "[realtime] Next sentence → speak_queue: %r",
                                        sentence[:80],
                                    )
                                    tts.speak_queue(sentence, turn_id=interaction_id, realtime_reply=True)
                            sentence_buf = tail if ready else ""

                execution_completed = (
                    getattr(realtime, "execution_completed", False) is True
                    and not look_replay
                )

                if look_replay and not look_replayed:
                    look_replayed = True
                    logger.info(
                        "[realtime] look: re-committing turn audio so the fresh "
                        "frame joins the replayed turn"
                    )
                    for _frame in rt_audio_buffer:
                        realtime.append_audio(_frame, **({"turn": audio_turn} if audio_turn is not None else {}))
                    text_parts = []
                    sentence_buf = ""
                    first_sentence_sent = False
                    leak_filter = CoTLeakFilter(reply_lang)
                    continue

                produced: bool = (
                    delegated
                    or rejected
                    or foreign_suppressed
                    or first_sentence_sent
                    or native_started
                    or bool("".join(text_parts).strip())
                )
                if produced or attempt >= max_retries:
                    break
                attempt += 1

            transcript = clean_transcript(strip_markers("".join(text_parts)), reply_lang)
            if (not native and not first_sentence_sent and not delegated
                    and not look_replayed and not transcript and execution_completed
                    and getattr(realtime, "intentional_silence", False) is True):
                # Gemini completed a marker-only silent decision. Reuse the
                # rejection route (including its feature flag and KPI exclusion)
                # instead of sending a non-request to main as an empty failure.
                rejected = True
                execution_completed = False
                logger.info("[realtime] Completed <no speech> decision — treating as rejected input")

            # Native playback owns the speaker for the whole turn — release it once all
            # frames are in (records transcript for STT echo cancel).
            if native_started:
                tts.native_play_end(transcript)
                native_started = False
                native_played = True

            if foreign_suppressed:
                route = ROUTE_FOREIGN_DROPPED
                transcript = ""
                _thinking_cue_clear()
                try:
                    from hal.routes.led import restore_led

                    restore_led()
                except Exception:
                    pass
            elif rejected:
                route = ROUTE_AI_REJECTED
                logger.info(
                    "[realtime] Model explicitly rejected turn — no main-agent dispatch"
                )
                _thinking_cue_clear()
                try:
                    from hal.routes.led import restore_led

                    restore_led()
                except Exception:
                    pass
            elif delegated:
                route = ROUTE_DELEGATED
                logger.info("[realtime] Model delegated → will forward to OS server")
            else:
                # Flush any remaining text that didn't end with a sentence boundary
                # (ElevenLabs path only — native mode never fills sentence_buf).
                remaining: str = leak_filter.filter_text(realtime_speech_text(sentence_buf, tts, strip_markers)) if realtime_visible_text(sentence_buf, tts, strip_markers) else ""
                if not native and remaining and tts is not None:
                    if not first_sentence_sent:
                        logger.info(
                            "[realtime] Final fragment → speak: %r", remaining[:80]
                        )
                        wait_filler.cancel()
                        if not tts.speak(remaining, turn_id=interaction_id, realtime_reply=True):
                            tts.speak_queue(remaining, turn_id=interaction_id, realtime_reply=True)
                        first_sentence_sent = True
                        _thinking_cue_clear()
                    else:
                        logger.info(
                            "[realtime] Final fragment → speak_queue: %r", remaining[:80]
                        )
                        tts.speak_queue(remaining, turn_id=interaction_id, realtime_reply=True)
                # Only claim the turn as HANDLED if the model actually SPOKE. An empty
                # result (receive() timed out, or native mode produced no audio) must
                # NOT be reported as handled.
                spoke = native_played if native else (first_sentence_sent or bool(transcript))
                if spoke:
                    handled = True
                    route = ROUTE_HANDLED
                    # Label this `agent_reply`, not `transcript`: it is what Moon
                    # SAID, not what the user said. Elsewhere `transcript` means the
                    # user's STT, so reusing the word here reads as role-reversed.
                    logger.info(
                        "[realtime] Chit-chat complete — agent_reply=%r",
                        transcript[:200] if transcript else "(empty)",
                    )
                    if save_history and (combined or transcript):
                        realtime.save_turn(
                            user_text=combined or "(audio only)",
                            agent_text=transcript or "(audio only)",
                        )
                else:
                    route = ROUTE_NO_OUTPUT
                    # No spoken output from the realtime agent (empty / timeout). Do NOT
                    # claim a forward here — whether the turn actually reaches the OS
                    # server is decided by the caller's `if combined:`.
                    logger.info(
                        "[realtime] No realtime output (empty / timeout) — "
                        "turn falls back to OS server only if STT produced a transcript"
                    )
                    # Dead turn — don't leave the thinking face hanging, and return the
                    # strip to the user's color (idle is a background emotion, so the
                    # clear alone leaves the forced purple pulse running).
                    _thinking_cue_clear()
                    try:
                        from hal.routes.led import restore_led

                        restore_led()
                    except Exception:
                        pass
        except Exception as e:
            execution_completed = False
            logger.warning(
                "[realtime] Processing failed: %s — will forward to OS server", e
            )
            # Release the speaker if native playback was mid-flight (avoids a
            # stuck TTS lock / native_mode flag).
            if native_started:
                try:
                    tts.native_play_end(transcript)
                except Exception:
                    pass
                native_started = False
            _thinking_cue_clear()
            delegated = True
            route = ROUTE_ERROR
        finally:
            wait_filler.cancel()
    elif hal_config.REALTIME_ENABLED and noise_turn:
        route = ROUTE_NOISE_DROPPED
        logger.info(
            "[realtime] Skipping commit — noise turn, not committing to model "
            "(stt=%r, require_transcript=%s, dur=%.2fs, min=%.2fs, silero_speech=%s)",
            combined[:40] if combined else "(empty)",
            hal_config.REALTIME_REQUIRE_TRANSCRIPT,
            buf_duration,
            hal_config.REALTIME_MIN_COMMIT_DURATION_S,
            audio_is_speech,
        )
        # The dropped turn's audio already streamed into the session's open manual-VAD
        # activity and would be billed with (and can confuse) the NEXT committed turn.
        if rt_audio_buffer and realtime.available:
            try:
                if realtime.discard_open_activity("noise-drop"):
                    logger.info(
                        "[realtime] Discarded open activity after noise drop (fresh session)"
                    )
            except Exception:
                logger.exception("[realtime] noise-drop discard failed")
    elif hal_config.REALTIME_ENABLED:
        route = ROUTE_UNAVAILABLE
        logger.warning(
            "[realtime] Enabled but agent not available — falling back to OS server"
        )

    return RealtimeTurnResult(
        delegated=delegated,
        handled=handled,
        transcript=transcript,
        delegate_msg=delegate_msg,
        handoff_context=handoff_context,
        route=route,
        rejected=rejected,
        execution_completed=execution_completed,
    )
