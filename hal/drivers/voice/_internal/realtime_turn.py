"""Realtime agent turn handling — extracted from VoiceService._stream_session.

Given the audio already streamed to the realtime model for one speaking turn,
drive the turn (commit → stream output → speak sentences → delegate/handle) and
report the outcome. Pure helper: it touches no VoiceService state beyond the
orchestrator / TTS handles and the marker-stripper passed in.
"""

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

# Clause boundaries the first chunk of a turn may be cut at — see
# split_first_chunk. Complete sentences bypass the early cut.
CLAUSE_ENDS = (",", ";", ":", "—", "，", "；", "：", "、")
# Below this many characters a clause is a runt ("Ừ,", "Sure,", "Vâng,") —
# speaking it alone sounds like a stutter, and the pause it buys is not worth
# it. 8, because a natural opener in a non-English reply is short: "Chào bạn,"
# is 9 characters and is exactly the greeting worth getting out early, while
# every grunt this must reject is 5 or fewer.
FIRST_CHUNK_MIN_CHARS = 8


def split_first_chunk(buf: str) -> tuple[str, str]:
    """Split the turn's FIRST utterance into (speak_now, keep_buffering).

    The user is sitting in silence until the first audio, and the model's
    opening sentence can run 20+ words. Cutting it at the last clause boundary
    lets TTS start on the head while the tail is still streaming; the tail is
    queued behind it, so the reply is still spoken in order.

    Returns ("", buf) when there is nothing worth speaking yet — no boundary and
    the buffer still under the cap, or a boundary too early to be a phrase.
    """
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
                # A colon followed by '/' belongs to a URL, not a clause.
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


def split_completed_prefix(buf: str) -> tuple[str, str]:
    """Release completed sentences bundled with the next unfinished sentence.

    Keep raw tags and the exact tail for the next chunk. Incomplete tags,
    numbers, initials and common abbreviations remain buffered conservatively.
    The existing end-of-buffer sentence path handles terminal punctuation.
    """
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
            cut = i + 1
    if depth or not cut or not buf[cut:].strip():
        return "", buf
    return buf[:cut], buf[cut:]


# How a turn resolved, for the single routing log line in dispatch_turn. Every
# value except HANDLED or AI_REJECTED means the main agent answers this turn.
ROUTE_HANDLED = "realtime_handled"          # realtime spoke; main agent stays silent
ROUTE_DELEGATED = "delegated"               # the model asked to hand off
ROUTE_AI_REJECTED = "ai_rejected"           # model explicitly rejected this non-user turn
ROUTE_NO_OUTPUT = "realtime_no_output"      # committed, but nothing came back (timeout / dead WS)
ROUTE_ERROR = "realtime_error"              # the turn raised; forwarded instead of lost
ROUTE_UNAVAILABLE = "realtime_unavailable"  # no live session to commit to
ROUTE_NOISE_DROPPED = "noise_dropped"       # never committed — noise guard rejected it
ROUTE_FOREIGN_DROPPED = "foreign_dropped"   # reply in a script the device can't speak
ROUTE_NOT_STARTED = "realtime_not_started"  # realtime off, or no turn was opened this capture


def _reply_language_name() -> str:
    """Resolve the device's reply language to a human name (e.g. "Vietnamese").

    Reads stt_language from config.json and maps it via the same table the system
    prompt uses, so the per-turn reminder and the prompt stay in sync. Returns ""
    when no language is configured — we don't force a default, mirroring the
    prompt's `_load_language() or "English"` only when one is actually set.
    """
    from hal.config import _os_cfg_get
    from hal.realtime.context_manager.base import ContextManagerBase

    code: str = (_os_cfg_get("stt_language", "") or "").strip()
    if not code:
        return ""
    return ContextManagerBase.LANGUAGE_NAMES.get(code, code)


def _thinking_cue_start() -> None:
    """Show `thinking` while the realtime model works on the committed turn.

    In-session turns (no delegate) had NO visual state between the listening
    cue and the first reply audio — 1-3s of a lamp that looks frozen. Full
    in-process emotion call, same path the agents use.
    """
    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=presets.EMO_THINKING))
        # thinking is a background emotion, so the route's LED path skips it
        # whenever the user has a saved color (guard against per-message hook
        # spam). This cue is deliberate and always cleared — force the pulse so
        # the wait is visible. User-LED-off still wins inside.
        hal_app_state._thinking_cue_active = True
        hal_app_state._apply_emotion_led_display(
            presets.EMO_THINKING, 0.7, force_led=True
        )
    except Exception as e:
        logger.warning("[realtime] thinking cue failed: %s", e)


class _WaitFiller:
    """Speak one dead-air filler if the realtime model keeps the user waiting.

    The thinking cue above covers the wait visually; this covers it audibly.
    The phrase fires only after REALTIME_FILLER_DELAY_S with still no output, so
    a reply that starts sooner is never interrupted by one.

    How often that happens is a property of the model, not of this class. On a
    fast one it catches only the slow class (a turn grounded with Google Search
    emits nothing until the search returns); where first-sentence latency never
    drops below a few seconds — measured on lamp-0c89 — it fires on effectively
    every turn, and the delay is what decides whether the device answers like
    someone listening or like someone lagging.

    os-server picks the phrase and speaks it from its WAV cache (POST
    /api/sensing/filler): keeping the pools there means the realtime wait and
    the main-agent wait draw from the same voice, and neither drifts when the
    other is edited. The filler is spoken interruptible, so the model's first
    sentence cuts it off mid-word rather than queueing behind it.

    One filler per turn, by construction: the timer is single-shot. cancel() is
    idempotent and safe to call from any exit path, including exceptions.
    """

    def __init__(self, owner: str = "") -> None:
        self._timer: Optional[threading.Timer] = None
        self._fired = False
        # Voice metrics ownership: which utterance this "one moment" is for. The
        # tag rides the filler request and comes back on the /voice/speak
        # call os-server makes, so the played phrase is attributed instead of
        # landing as unclaimed audio (which never counts as a response).
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
    """Return to idle when the reply starts (or the turn produced nothing) —
    ONLY if the face still shows our `thinking`, so an emotion the model
    expressed via the express_emotion tool is never stomped."""
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

    The caller must send this before streaming audio for the turn. Gemini 2.5
    native-audio can close the websocket with 1011 when clientContent is
    interleaved inside an open audio activity window.

    ``speaker`` is the VOICE speaker-ID display name (e.g. "Darren") for this
    turn, resolved by the speaker-ID prepass just before this call. When present
    it is authoritative for who the model should address and OVERRIDES the
    face-derived ``current_user`` (the two identities are separate: voice = who
    is speaking, face = who is seen). When None (no voice ID this turn — unknown
    / gate-reject / no transcript) we fall back to the face ``current_user``.
    """
    turn_ctx: list[str] = [
        f"Time: {device_now().strftime('%Y-%m-%d %H:%M:%S %A')}",
    ]
    # Per-turn language reminder. The system prompt already locks the language, but
    # Google Search grounding pulls English source text into context and can drag
    # the spoken reply into English. A reminder right next to the turn (closest to
    # generation) is the strongest lever.
    lang_name: str = _reply_language_name()
    if lang_name:
        turn_ctx.append(
            f"Reply language: {lang_name} (answer ONLY in {lang_name}, "
            "even if a search result or any context is in another language)"
        )
    if speaker:
        # Voice speaker-ID wins: reuse the same "Current user:" slot the model
        # already acts on, and phrase it so it overrides any stale identity in
        # session memory (e.g. the previously-seen face).
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
    """Late identity correction for a turn whose context was already sent.

    In always-listening mode the turn context goes out when the mic session
    opens — before any audio exists, so voice speaker-ID cannot have run yet and
    ``build_turn_context()`` necessarily falls back to the face-derived
    ``current_user``. The voiceprint is only resolvable once capture is complete,
    which is AFTER that send. This line is sent just before the audio commit to
    override the identity already in the turn, so the model addresses the person
    who actually spoke rather than the last face it saw (or whoever its session
    memory still thinks it is talking to).

    Phrased as an explicit correction, not a second "Current user:" line: two
    contradictory assignments in one turn let the model pick either one.
    """
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
    # Why the turn ended up where it did — one of ROUTE_*. Only the delegated
    # path used to log a reason, so every OTHER way of reaching the main agent
    # (no output, dead session, dropped noise, realtime off) was invisible and
    # indistinguishable in the journal. dispatch_turn logs this on every turn.
    route: str = ROUTE_NOT_STARTED
    # This is deliberately separate from `route`: an empty/no-output turn must
    # retain main-agent fallback. Only an explicit reject_turn tool call sets it.
    rejected: bool = False
    # Observation only: the provider confirmed execution finished, not correctness.
    execution_completed: bool = False
    handoff_context: str = ""


def should_drop_realtime_rejection(rt: RealtimeTurnResult) -> bool:
    """Return whether the explicit AI rejection may suppress downstream dispatch.

    This isolated policy gate is intentionally the only behavior-changing check.
    Disable ``HAL_REALTIME_AI_REJECT_FILTER`` to immediately restore the old
    fallback for every turn, including previously explicit rejections.
    """
    return hal_config.REALTIME_AI_REJECT_FILTER and rt.rejected


def should_drop_downstream_turn(rt: RealtimeTurnResult) -> bool:
    """Return whether a terminal guard/model decision must stop OS dispatch.

    A noise guard rejection is terminal even when STT fabricated a short word:
    forwarding that word would undo the guard and let room noise reach the main
    agent. The explicit AI rejection remains separately configurable above.
    """
    return (
        rt.route in (ROUTE_NOISE_DROPPED, ROUTE_FOREIGN_DROPPED)
        or should_drop_realtime_rejection(rt)
    )


def should_dispatch_to_main(
    wakeword_enabled: bool,
    wakeword_authorized: bool,
) -> bool:
    """Return whether the finalized STT turn must reach the main agent.

    Wake-word mode suppresses ambient speech until STT has armed the turn. Once
    authorized by a final wake-word match or an active follow-up focus window,
    every turn reaches dispatch: a realtime-handled turn becomes a
    `voice_agent_handled` synchronization event, while unavailable, silent,
    failed, and delegated turns take the normal main-agent path. This keeps
    memory and one-turn vision handoff behavior identical to always-listening.
    """
    if not wakeword_enabled:
        return True
    return wakeword_authorized


def needs_noise_guard(combined: str) -> bool:
    """Return whether the Silero voiced-ratio guard must run for this transcript.

    Always for an empty transcript. Also for a very short one: STT fabricates a
    filler word out of room noise and reports full confidence for it, which used
    to bypass every guard here and commit a turn of pure noise to the model.
    """
    if not combined:
        return True
    max_words: int = hal_config.REALTIME_NOISE_GUARD_MAX_WORDS
    return 0 < len(combined.split()) <= max_words


def should_arm_realtime_wait_filler(combined: str) -> bool:
    """Return whether this realtime turn may receive an audible wait filler.

    Short STT output is the ambiguity class that the realtime model reviews
    with ``reject_turn``. A filler would speak before that verdict arrives and
    turn an otherwise silent rejection into an audible response.
    """
    return not needs_noise_guard(combined)


def should_defer_speaker_id_prepass(combined: str) -> bool:
    """Return whether speaker ID can wait for the explicit AI rejection verdict.

    The recognizer is an external inference call. It provides no value for a
    short turn the realtime model explicitly rejects, while delaying audio
    commit by hundreds of milliseconds. A non-rejected turn still resolves its
    identity before downstream dispatch.
    """
    return (
        hal_config.REALTIME_ENABLED
        and hal_config.REALTIME_AI_REJECT_FILTER
        and bool(combined)
        and needs_noise_guard(combined)
    )


def is_nonactionable_transcript(combined: str) -> bool:
    """Return whether the whole transcript is only backchannel/filler words.

    A turn like "okay", "yeah", "uh", "one sec" carries no request. The realtime
    model tends to stay silent on these rather than call reject_turn, so the turn
    falls through to a dead main-agent turn (burns a full LLM turn for nothing,
    device-observed 2026-09-18). This deterministic backstop drops it. Whole-
    utterance match only: a filler inside a longer request ("okay do it") has a
    non-filler token and is NOT dropped. Empty transcript is handled elsewhere.
    """
    fillers = hal_config.REALTIME_NONACTIONABLE_FILLERS
    if not combined or not fillers:
        return False
    norm: str = re.sub(r"[^\w\s-]", " ", combined.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return False
    if norm in fillers:  # multi-word filler ("one sec")
        return True
    return all(tok.strip("-") in fillers for tok in norm.split())


# Scripts an English (or any non-CJK/non-Vietnamese) device must never speak.
# From noise or a language switch the model sometimes hallucinates a reply in
# Japanese/Korean/Chinese (device-observed on 3.1) or Vietnamese. These are
# unmistakable by codepoint, so a wrong-script reply is dropped deterministically
# rather than spoken. Only catches a WRONG-SCRIPT reply — a model that answers
# foreign speech by translating INTO the device language is not caught here.
_CJK_KANA_HANGUL = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
# đ Đ + Vietnamese base vowels + Latin Extended Additional (precomposed vi vowels).
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
    # A finalized transcript that is only acknowledgment/filler words is not a
    # request — drop it here so it reaches neither the realtime model nor main.
    if is_nonactionable_transcript(combined):
        return True
    # A short transcript that Silero judged non-speech is an STT fabrication over
    # noise, not a short command — drop it whatever REQUIRE_TRANSCRIPT says. Only
    # reachable when the caller actually ran the guard (audio_is_speech defaults
    # to True), so a missing check still never drops a turn.
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
    """Commit the captured audio to the realtime agent and stream its reply.

    ``wait_filler`` lets the caller arm the dead-air filler BEFORE the session
    handshake that precedes this call: on the wake-word path the filler used to
    start its 0.5 s clock only here, after 2-3 s of Gemini reconnect, so the
    user's first audible acknowledgement landed at 4-5 s (device-observed
    lamp-dbda 2026-09-18). One filler per turn still holds: arm() is idempotent.

    Runs even if the STT transcript is empty — the model has the raw audio.
    Speaks complete sentences as they arrive. Returns how the turn resolved so
    the caller can forward (delegate), suppress (handled), or fall back.
    """
    delegated = False
    handled = False
    execution_completed = False
    rejected = False
    foreign_suppressed = False  # reply came back in a script the device can't speak
    transcript = ""
    delegate_msg = ""
    handoff_context = ""
    route = ROUTE_NOT_STARTED
    native = (hal_config.REALTIME_NATIVE_AUDIO or native_voice(tts) is not None) and tts is not None
    native_started = False  # cleanup guard: True between begin and end
    native_played = False    # did native audio actually play this turn (for handled)

    if combined and (harness_followup if harness_followup is not None else harness_followup_active()):
        logger.info("[realtime] Harness follow-up active — delegating without realtime reply")
        return RealtimeTurnResult(delegated=True, delegate_msg=combined, route=ROUTE_DELEGATED)

    # Noise/false-trigger guard: a session with no STT transcript is not worth a
    # model turn — committing it makes the model answer silence/noise (spurious
    # self-talk + wasted tokens), which then desyncs onto a later real turn.
    #
    # REQUIRE_TRANSCRIPT (default): drop ANY empty-STT turn. The Silero signals
    # below only reject non-speech; real human speech that nova-3 missed (short
    # utterances below its floor) is voiced and would otherwise commit as raw
    # audio, which Gemini fills with an invented greeting. "No transcript" → don't
    # speak. A turn with a transcript always commits.
    #
    # When REQUIRE_TRANSCRIPT is off, fall back to the Silero-gated audio-only
    # path: an empty-STT turn still commits unless (1) it's a sliver of audio (just
    # the VAD pre-roll, below the duration floor), or (2) Silero VAD judged the
    # FULL buffer non-speech (`audio_is_speech` computed by the caller, default
    # True so a missing check never drops a turn).
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
        # Audible half of the same wait (see _WaitFiller). Armed here rather
        # than per attempt so a 1011 retry does not restart the clock — from the
        # user's side it is one uninterrupted silence.
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
            # 2.5-native-audio sessions, so a turn that follows a pause lands on a
            # dead session and Gemini returns WS 1011 with NO output. The proxy
            # serves ACTIVE sessions fine (continuous talking works), so on a
            # no-output turn reconnect a FRESH session and REPLAY this turn's
            # already-captured audio IMMEDIATELY (no idle wait) — turning a
            # post-pause turn into an active one. Audio lives in rt_audio_buffer.
            # Gemini only; other providers retry 0 times.
            # Replay only for 2.5 native-audio (the model with the idle-resume WS
            # 1011). 3.1 handles idle→resume fine, so retries=0 there — no churn,
            # no 6-9s dead-air on a no-output turn. See gemini_needs_idle_workaround.
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
            look_replayed: bool = False  # one look-replay per turn (loop guard)
            # Gemini 3.1 sometimes leaks its chain of thought into the reply text
            # (thinking can't be disabled on that model) — filter it out of both
            # the spoken sentences and the forwarded transcript. See
            # cot_leak_filter.py for the mechanism and evidence.
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

                # Drop any output still queued from a previous turn so this turn
                # only reads its OWN response (stale async replies desync onto a
                # later turn → "talks on its own" + double TTS).
                # Time-to-first-* for this turn. Without these two numbers the
                # only measurable span was commit → first spoken sentence (3-6s
                # on lamp-0c89), which mixes the model's own latency with how
                # long its first sentence takes to stream. Tuning either one
                # blind is guesswork.
                t_commit: float = time.monotonic()
                first_output_logged: bool = False

                execution_completed = False
                look_replay: bool = False
                audio_turn, outputs = _commit_turn_output(realtime, rt_audio_buffer, audio_turn)
                if not thinking_started:
                    # Start provider work before synchronous hardware feedback.
                    # Keep emotion on this thread: a detached cue could finish
                    # after reply/cancel and overwrite the next turn's emotion.
                    # Retry/look replay is still the same user-visible wait.
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
                        # Model called look and a fresh frame was sent. The Live
                        # API queues mid-turn frames for the NEXT turn, so the
                        # generator stopped this one — re-commit the same audio
                        # below and the frame joins the replayed turn.
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
                        # No main-agent handoff follows an explicit rejection.
                        # Cancel the filler and leave the thinking LED in the
                        # same clean state as any other completed empty turn.
                        wait_filler.cancel()
                        break
                    if delegated:
                        continue
                    # Native voice: play the model's OWN audio straight to the speaker.
                    if native and isinstance(output, RTAudioOutput):
                        if not native_started:
                            # Retain the leading frames while speaker admission
                            # waits for a filler/protected utterance to finish.
                            native_pending.append(output.audio)
                            native_pending_samples += len(output.audio)
                            if native_pending_samples > realtime.output_sample_rate * 30:
                                logger.warning("[realtime] Native speaker unavailable for 30s of audio — abandoning reply")
                                break
                            # Cancel the timer before contending for the speaker;
                            # an already-playing filler is preempted by admission.
                            wait_filler.cancel()
                            native_started = tts.native_play_begin(
                                realtime.output_sample_rate,
                                # Explicit ownership for voice metrics: this audio
                                # answers THIS utterance, nothing else.
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
                        # Wrong-script reply (e.g. noise hallucinated as JP/KR, or
                        # a Vietnamese reply on an English device): never speak it.
                        # Detected on the accumulated text so the first foreign
                        # chunk trips it before any audio goes out. Drop the whole
                        # turn (handled below) rather than forwarding to main.
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
                        # Nothing spoken yet: cut the opening at a clause
                        # boundary rather than making the user wait out a whole
                        # sentence. Only ever once per turn (see split_first_chunk).
                        if tts is not None and not first_sentence_sent:
                            head, rest = split_first_chunk(sentence_buf)
                            head = leak_filter.filter_text(strip_markers(head)) if head else ""
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
                        # Trailing voice/HW tags are not spoken punctuation.
                        # Keep the raw buffer until ready so split tags reassemble.
                        sentence = strip_markers(sentence_buf)
                        complete = sentence.rstrip().endswith(SENTENCE_ENDS)
                        ready, tail = ("", "") if complete else split_completed_prefix(sentence_buf)
                        if ready:
                            sentence = strip_markers(ready)
                        if tts is not None and sentence.rstrip().endswith(SENTENCE_ENDS):
                            sentence = leak_filter.filter_text(sentence)
                            if sentence:
                                if not first_sentence_sent:
                                    logger.info(
                                        "[realtime] First sentence → speak (+%.2fs "
                                        "after commit): %r",
                                        time.monotonic() - t_commit,
                                        sentence[:80],
                                    )
                                    # speak() returns False when another
                                    # non-interruptible TTS holds the speaker
                                    # (ambient nudge racing the turn) — queue
                                    # the reply instead of losing it entirely.
                                    # Cancel BEFORE speaking: a filler that fires
                                    # in the gap between here and playback would
                                    # interrupt the very sentence it exists to
                                    # cover (both are interruptible).
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

                # Look-replay: re-append this turn's audio to the SAME session
                # (unlike the 1011 recovery below, which needs a fresh one) and
                # loop — flush_output + commit_audio at the top open a new turn
                # that picks up the queued camera frame. The new user activity
                # cancels the pending tool-call turn server-side. Guarded to
                # once per turn; a second signal (shouldn't happen — the replay
                # turn's look hits the reuse path) falls through to the normal
                # exit so it can't loop forever.
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

                # A WS-1011 failure yields NOTHING (no audio spoken yet), so a
                # retry is safe. Stop as soon as the turn produced real output.
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

            # Clean the transcript with FRESH filter state (it re-reads the whole
            # turn from the top): this is what gets forwarded as [REPLY], saved to
            # realtime memory, and shown in web chat — a leak here re-enters the
            # model's context next turn and self-reinforces.
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

            # Native playback owns the speaker for the whole turn — release it
            # once all frames are in (records transcript for STT echo cancel).
            # Reset native_started so a later exception's cleanup can't double-end;
            # native_played records that audio actually played (for `spoke` below).
            if native_started:
                tts.native_play_end(transcript)
                native_started = False
                native_played = True

            if foreign_suppressed:
                # Wrong-script reply: drop the whole turn. Not spoken, not saved,
                # not forwarded to main (main would just re-answer the same foreign
                # input). Terminal like the noise guard, independent of the
                # AI-reject filter.
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
                remaining: str = leak_filter.filter_text(strip_markers(sentence_buf))
                if not native and remaining and tts is not None:
                    if not first_sentence_sent:
                        logger.info(
                            "[realtime] Final fragment → speak: %r", remaining[:80]
                        )
                        # Same cancel-then-busy-fallback as the first-sentence
                        # site above.
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
                # Only claim the turn as HANDLED if the model actually SPOKE.
                # Native mode → audio actually played (native_played); ElevenLabs
                # mode → a sentence was synthesized OR a transcript exists. An empty
                # result (receive() timed out, or native mode produced no audio) must
                # NOT be reported as handled: that sends [HANDLED] with an empty
                # [REPLY], OpenClaw's input-branching reads it as "already answered"
                # and stays silent. Leaving handled False (delegated also False) falls
                # through to the normal forward below so the main agent answers.
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
                    # Save turn to realtime memory
                    if save_history and (combined or transcript):
                        realtime.save_turn(
                            user_text=combined or "(audio only)",
                            agent_text=transcript or "(audio only)",
                        )
                else:
                    route = ROUTE_NO_OUTPUT
                    # No spoken output from the realtime agent (empty / timeout). Do
                    # NOT claim a forward here — whether the turn actually reaches the
                    # OS server is decided by the caller's `if combined:`. A pure
                    # noise turn with empty STT is correctly dropped.
                    logger.info(
                        "[realtime] No realtime output (empty / timeout) — "
                        "turn falls back to OS server only if STT produced a transcript"
                    )
                    # Dead turn — don't leave the thinking face hanging, and
                    # return the strip to the user's color (idle is a
                    # background emotion, so the clear alone leaves the forced
                    # purple pulse running). restore_led is TTS-guarded, so a
                    # reply that somehow started speaking keeps its wave.
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
            # The cue is normally handed over to the delegate path (the main
            # agent's wait is longer, and its own hook re-fires thinking on the
            # forwarded turn). A crashed turn is not that handover: nothing in
            # this process is still driving the face, so hand it back to the
            # user's state instead of leaving the pulse claimed. If the forward
            # below does reach the agent, the hook paints thinking again.
            _thinking_cue_clear()
            delegated = True  # fall through to OS server on error
            route = ROUTE_ERROR
        finally:
            # Covers every exit — reply spoken, delegate, empty turn, exception.
            # A timer left armed here would fire into the NEXT turn's silence.
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
        # The dropped turn's audio already streamed into the session's open
        # manual-VAD activity and would be billed with (and can confuse) the
        # NEXT committed turn. Swap in a fresh session — this turn is dead
        # air, so nobody is waiting on the ~1s handshake.
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
