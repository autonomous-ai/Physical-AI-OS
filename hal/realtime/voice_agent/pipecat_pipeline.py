"""The Pipecat side of the `pipecat_v1` provider.

Everything that imports `pipecat` lives here, so `pipecat_v1.py` (the
`VoiceAgentBase` contract: threads, queues, turn and generation bookkeeping)
stays importable and unit-testable on a host without the package.

Shape of the pipeline, one per provider session::

    queue_frame ─▶ HALSTTService ─▶ user aggregator ─▶ OpenAILLMService ─▶ EventSink ─▶ assistant aggregator
                   (device STT)      (VAD / turns)      (text + tools)       (→ agent)

- Audio enters through `PipelineWorker.queue_frame` as `InputAudioRawFrame`;
  there is no transport. HAL owns the mic and the speaker.
- Turn detection is the user aggregator's. Live mode: Silero VAD starts the
  turn, Smart Turn v3 (or a silence timeout) ends it. Turn-based mode: HAL has
  already bracketed the utterance, so the agent *proposes* the start on the
  first frame and the stop on `commit_audio`, and the aggregator finalizes as
  soon as the STT final lands.
- The LLM only ever produces text. Tool calls are bridged to the orchestrator:
  the handler emits `FunctionCallOutput` and waits for the matching
  `FunctionCallResultInput`, whose `trigger_response` becomes Pipecat's
  `run_llm`.
- `EventSink` turns pipeline frames into plain method calls on the agent
  (`_ev_*`), which is where the HAL event contract is produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    MetricsFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData, TTFBMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.types import NOT_GIVEN
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import (
    ExternalUserTurnStopStrategy,
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    ExternalUserTurnStrategies,
    UserTurnStrategies,
)
from pipecat.workers.runner import WorkerRunner

from hal.realtime.voice_agent.pipecat_stt import HALSTTService, STTFinalizeFrame

if TYPE_CHECKING:
    from hal.drivers.voice.stt.provider import STTProvider
    from hal.realtime.config import PipecatV1Config
    from hal.realtime.voice_agent.pipecat_v1 import PipecatV1Agent

logger = logging.getLogger(__name__)

# Appended to the system instruction when delegate_to_main is registered, and
# re-injected as a system message right before every user utterance
# (PipelineHandle.remind_tools). The orchestrator's instructions put the tool
# rules first and then ~10k tokens of identity / memory / history behind them;
# on lamp-ee17 (2026-09-18) Qwen 3.6-35B-A3B answered "please play some music"
# with "You got it, what vibe?" instead of delegating whenever that boot's
# realtime-memory summary was long, while the same pipeline with a shorter
# summary delegated. A short rule adjacent to the utterance restores the
# priority for a model that weighs recency; ~60 tokens per turn. It rides as a
# USER-role line (like HAL's `[TURN CONTEXT]`): the Qwen relay rejects a system
# message anywhere but first ("System message must be at the beginning", 400).
_TOOL_RULE = (
    "[RULE] "
    "Any request to play, stop, move, turn, look, find, change, set, control, "
    "remind, schedule, remember, or run something is an ACTION: call "
    "`delegate_to_main` with the user's own words and write NO text. Only "
    "conversation and knowledge questions are answered in text."
)
_TOOL_TAIL = "\n\n## FINAL RULE (highest priority)\n" + _TOOL_RULE


class _CommittedTurnStopStrategy(ExternalUserTurnStopStrategy):
    """Turn-based mode: the turn is over the moment the committed STT final lands.

    HAL already decided end-of-speech (its own VAD) and the agent closed the STT
    session on commit, so the final transcript that follows IS the whole turn.
    The stock strategy would still wait its aggregation `timeout` (0.5 s) for
    more text; that wait is pure latency here.
    """

    async def _handle_transcription(self, frame: TranscriptionFrame):
        await super()._handle_transcription(frame)
        if frame.finalized and not self._user_speaking and self._turn_open:
            await self._trigger_user_turn_stopped()


class _BusyAwareMinWordsStrategy(MinWordsUserTurnStartStrategy):
    """MinWords keyed on the agent's LLM state instead of `BotStartedSpeakingFrame`.

    The stock strategy applies `min_words` only while the bot is speaking, and
    it learns that from the output transport's BotStarted/StoppedSpeaking
    frames — which this pipeline has none of (HAL owns the speaker), so it
    degraded to "one word opens a turn". A one-word burst right after a
    question then interrupted the reply mid-generation (lamp-ee17,
    2026-09-18). The window that needs protecting is exactly "the model is
    generating", which the agent knows; outside it a single word still opens a
    turn, so "yes" / "stop" keep working.
    """

    def __init__(self, agent: PipecatV1Agent, *, min_words: int, **kwargs: Any) -> None:
        super().__init__(min_words=min_words, **kwargs)
        self._agent = agent

    async def _handle_transcription(self, frame):  # type: ignore[override]
        self._bot_speaking = self._agent.llm_busy
        return await super()._handle_transcription(frame)


class EventSink(FrameProcessor):
    """Pass-through processor that reports pipeline events to the agent."""

    def __init__(self, agent: PipecatV1Agent) -> None:
        super().__init__(name="hal-event-sink")
        self._agent = agent

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._dispatch(frame)
        await self.push_frame(frame, direction)

    def _dispatch(self, frame: Frame) -> None:
        agent = self._agent
        if isinstance(frame, LLMTextFrame):
            agent._ev_text(frame.text)
        elif isinstance(frame, LLMFullResponseStartFrame):
            agent._ev_response_started()
        elif isinstance(frame, LLMFullResponseEndFrame):
            agent._ev_response_ended()
        elif isinstance(frame, FunctionCallsStartedFrame):
            agent._ev_calls_started([fc.tool_call_id for fc in frame.function_calls])
        elif isinstance(frame, FunctionCallResultFrame):
            agent._ev_call_result(frame.tool_call_id, frame.run_llm)
        elif isinstance(frame, UserStartedSpeakingFrame):
            agent._ev_user_turn_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            agent._ev_user_turn_stopped()
        elif isinstance(frame, InterruptionFrame):
            agent._ev_interruption()
        elif isinstance(frame, StartFrame):
            agent._ev_started()
        elif isinstance(frame, ErrorFrame):
            agent._ev_error(frame.error, frame.fatal)
        elif isinstance(frame, MetricsFrame):
            for data in frame.data:
                if isinstance(data, TTFBMetricsData):
                    agent._ev_ttfb(data.processor, data.value)
                elif isinstance(data, LLMUsageMetricsData):
                    usage = data.value
                    agent._ev_llm_usage(
                        getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0,
                    )


class PipelineHandle:
    """Thread-safe control surface over one running pipeline.

    Built and run on the agent's private asyncio loop; every method here may be
    called from the agent's send/recv threads.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        worker: PipelineWorker,
        run_future: asyncio.Future,
        stt: HALSTTService,
        sample_rate: int,
        remind: bool = False,
    ) -> None:
        self._loop = loop
        self._worker = worker
        self._run_future = run_future
        self._stt = stt
        self._sample_rate = sample_rate
        self._remind = remind

    @property
    def alive(self) -> bool:
        return not self._run_future.done()

    def _submit(self, coro) -> None:
        if self._loop.is_closed():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def queue_audio(self, pcm16: bytes) -> None:
        self._submit(
            self._worker.queue_frame(
                InputAudioRawFrame(audio=pcm16, sample_rate=self._sample_rate, num_channels=1)
            )
        )

    def propose_user_turn_start(self) -> None:
        self._submit(self._worker.queue_frame(ProposedUserStartedSpeakingFrame()))

    def propose_user_turn_stop(self) -> None:
        self._submit(self._worker.queue_frame(ProposedUserStoppedSpeakingFrame()))

    def append_context(self, text: str) -> None:
        """Silent context (`[TURN CONTEXT]`, `[TTS HISTORY]`): recorded, no run."""
        self._submit(
            self._worker.queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=False)
            )
        )

    def run_announcement(self, text: str) -> None:
        """Device-initiated turn (AnnounceInput): append it and run the LLM."""
        self._submit(
            self._worker.queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True)
            )
        )

    def remind_tools(self) -> None:
        """Re-state the action rule right before the utterance the aggregator is
        about to append (see _TOOL_RULE). No-op without delegate_to_main."""
        if not self._remind:
            return
        self._submit(
            self._worker.queue_frame(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": _TOOL_RULE}], run_llm=False)
            )
        )

    def finalize_stt(self) -> None:
        """Ordered behind the turn's audio (see STTFinalizeFrame)."""
        self._submit(self._worker.queue_frame(STTFinalizeFrame()))

    def stop(self, timeout_s: float) -> None:
        """Cancel the pipeline and wait for its run to unwind."""
        if not self._loop.is_closed() and not self._run_future.done():
            asyncio.run_coroutine_threadsafe(self._worker.cancel(), self._loop)
        try:
            self._run_future.result(timeout=timeout_s)
        except Exception:  # noqa: BLE001 — cancelled / already failed / timed out
            pass


def _tool_schema(tool: dict[str, Any]) -> FunctionSchema:
    params = tool.get("parameters") or {}
    return FunctionSchema(
        name=tool["name"],
        description=tool.get("description", ""),
        properties=params.get("properties") or {},
        required=list(params.get("required") or []),
    )


def _make_tool_handler(agent: PipecatV1Agent, name: str):
    async def handler(params: FunctionCallParams) -> None:
        arguments = json.dumps(params.arguments or {})
        future = agent._begin_tool_call(name, arguments, params.tool_call_id)
        try:
            output, run_llm = await future
        except asyncio.CancelledError:
            agent._abandon_tool_call(params.tool_call_id)
            raise
        try:
            result: Any = json.loads(output) if output else {}
        except (ValueError, TypeError):
            result = {"result": output}
        await params.result_callback(
            result, properties=FunctionCallResultProperties(run_llm=run_llm)
        )

    return handler


def _user_params(agent: PipecatV1Agent, cfg: PipecatV1Config, *, live: bool) -> LLMUserAggregatorParams:
    if not live:
        strategies = ExternalUserTurnStrategies(enable_interruptions=True)
        strategies.stop = [_CommittedTurnStopStrategy()]
        return LLMUserAggregatorParams(
            user_turn_strategies=strategies,
            user_turn_stop_timeout=cfg.turn_stop_timeout_s,
        )
    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=cfg.vad_confidence,
            start_secs=cfg.vad_start_secs,
            stop_secs=cfg.vad_stop_secs,
            min_volume=cfg.vad_min_volume,
        )
    )
    if cfg.smart_turn:
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

        stop = TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=cfg.smart_turn_stop_secs)
            )
        )
    else:
        stop = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=cfg.silence_timeout_s)
    # Start on transcribed words rather than the VAD onset when configured: a
    # far-field mic with residual echo produces one-word bursts that would
    # otherwise open a turn and cancel the reply being generated.
    start = [_BusyAwareMinWordsStrategy(agent, min_words=cfg.min_words)] if cfg.min_words > 0 else None
    return LLMUserAggregatorParams(
        vad_analyzer=vad,
        user_turn_strategies=UserTurnStrategies(start=start, stop=[stop]),
        user_turn_stop_timeout=cfg.turn_stop_timeout_s,
    )


async def build_and_run(
    agent: PipecatV1Agent,
    *,
    cfg: PipecatV1Config,
    tools: list[dict[str, Any]],
    stt_provider: STTProvider,
    live: bool,
) -> PipelineHandle:
    """Build the pipeline on the current loop and start running it.

    Returns once the worker is scheduled; the agent waits for `_ev_started`
    (the pipeline's `StartFrame`) before declaring the session connected.
    """
    loop = asyncio.get_running_loop()

    stt = HALSTTService(
        stt_provider,
        per_turn=not live,
        sample_rate=cfg.sample_rate,
        on_transcript=agent._ev_transcript,
        on_turn_finalized=agent._ev_stt_turn_finalized,
    )

    instructions = cfg.instructions or ""
    has_delegate = any(t.get("name") == "delegate_to_main" for t in tools)
    if has_delegate:
        instructions += _TOOL_TAIL
    llm = OpenAILLMService(
        api_key=cfg.api_key or "not-needed",
        base_url=cfg.base_url or None,
        settings=OpenAILLMService.Settings(
            model=cfg.model,
            system_instruction=instructions or None,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            # Qwen3 emits `reasoning` before `content` by default; Pipecat only
            # streams `content`, so thinking would be seconds of dead air.
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
            if cfg.disable_thinking
            else {},
        ),
        # A tool result that never comes (orchestrator gone) must not wedge the
        # LLM forever; the agent's own wait is shorter and answers with an error.
        function_call_timeout_secs=cfg.tool_result_timeout_s + 5.0,
    )
    for tool in tools:
        llm.register_function(tool["name"], _make_tool_handler(agent, tool["name"]))

    # The system prompt rides on the service (`system_instruction`); an initial
    # system message in the context is deprecated since Pipecat 1.9.
    context = LLMContext(
        [],
        tools=ToolsSchema(standard_tools=[_tool_schema(t) for t in tools]) if tools else NOT_GIVEN,
    )
    aggregators = LLMContextAggregatorPair(context, user_params=_user_params(agent, cfg, live=live))

    pipeline = Pipeline(
        [stt, aggregators.user(), llm, EventSink(agent), aggregators.assistant()]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=cfg.sample_rate,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=None,  # a device session idles for hours by design
        enable_rtvi=False,
        enable_turn_tracking=False,
        check_dangling_tasks=False,
    )
    runner = WorkerRunner(
        handle_sigint=False, handle_sigterm=False, check_dangling_tasks=False
    )
    # ErrorFrames travel UPSTREAM (push_error), so the sink downstream of the
    # LLM never sees one; the worker reports them here instead.
    @worker.event_handler("on_pipeline_error")
    async def _on_pipeline_error(_worker, frame):
        agent._ev_error(str(frame.error), bool(frame.fatal))

    run_future = loop.create_task(runner.run(worker))
    return PipelineHandle(
        loop=loop, worker=worker, run_future=run_future, stt=stt, sample_rate=cfg.sample_rate,
        remind=has_delegate,
    )
