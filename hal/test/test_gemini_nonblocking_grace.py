"""NON_BLOCKING tool grace spans the SDK's per-turn receive iterators."""

import asyncio
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from google.genai.live import AsyncSession

from hal.realtime.models import AudioOutput, FunctionCallOutput, OutputEvent, TextOutput, TurnDoneEvent
from hal.realtime.voice_agent import gemini_live
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _Session:
    # Exercise the installed SDK's iterator boundary: receive() stops immediately
    # after yielding turn_complete, even while more frames await on the socket.
    receive = AsyncSession.receive

    def __init__(self, messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.reads = 0
        self.tool_responses = []
        self.control_turns = []

    async def _receive(self):
        self.reads += 1
        return await self.messages.get()

    async def send_client_content(self, **kwargs):
        self.control_turns.append(kwargs)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)


def _terminal(*, generation=False):
    content = SimpleNamespace(
        grounding_metadata=None, model_turn=None, output_transcription=None,
        interrupted=False, turn_complete=not generation,
        generation_complete=generation,
    )
    return SimpleNamespace(usage_metadata=None, server_content=content,
                           tool_call=None, session_resumption_update=None,
                           go_away=None, voice_activity=None)


def _tool(name="delegate_to_main", call_id="c1"):
    return SimpleNamespace(
        usage_metadata=None, server_content=None,
        tool_call=SimpleNamespace(function_calls=[
            SimpleNamespace(name=name, args={"message": "play a song"}, id=call_id)]),
        session_resumption_update=None, go_away=None, voice_activity=None)


def _agent(messages, model="gemini-3.8-live-extended-thinking"):
    agent = object.__new__(GeminiLiveAgent)
    agent._session = _Session(messages)
    agent._config = SimpleNamespace(model=model)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._first_audio_received = False
    agent._last_audio_sent_at = None
    agent._activity_end_sent_at = None
    agent._pending_tool_calls = set()
    agent._gated_audio_frames = 0
    agent._turn_gen = 0
    agent._live_user_turn_id = "user-1"
    agent._user_transcript = "play a song"
    agent._awaiting_playback_turn_complete = False
    return agent


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0.02)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_OUTCOME_TIMEOUT_S", 0.03)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_RECV_QUEUE_TIMEOUT_S", 0.04)


def _receive(agent):
    # Bound the test independently so failure to finalize cannot hang pytest.
    asyncio.run(asyncio.wait_for(agent._async_receive_turn(), timeout=0.5))
    events = []
    while not agent._recv_queue.empty():
        events.append(agent._recv_queue.get_nowait())
    return events


def _assert_done(agent, events):
    terminals = [event for event in events if isinstance(event, TurnDoneEvent)]
    assert len(terminals) == 1
    assert terminals[0].user_turn_id == "user-1"
    assert agent._turn_done.is_set()
    assert agent._live_user_turn_id == ""


def _calls(events):
    return [event.output for event in events
            if isinstance(event, OutputEvent) and isinstance(event.output, FunctionCallOutput)]


@pytest.mark.parametrize("name", ["delegate_to_main", "reject_turn"])
def test_trailing_tool_call_caught_across_sdk_turn_boundary(name):
    agent = _agent([_terminal(), _tool(name)])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == [name]
    assert isinstance(events[-1], TurnDoneEvent)
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)


@pytest.mark.parametrize("generation", [False, True])
def test_no_tool_finalizes_after_grace_expires(generation):
    agent = _agent([_terminal(generation=generation)])
    events = _receive(agent)
    _assert_done(agent, events)
    assert agent._session.reads == 2
    assert not _calls(events)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed
    assert events[-1].user_transcript == "play a song"


def test_generation_complete_then_turn_complete_keeps_late_delegate_in_turn():
    agent = _agent([_terminal(generation=True), _terminal(), _tool()])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == ["delegate_to_main"]
    assert isinstance(events[-1], TurnDoneEvent)
    _assert_done(agent, events)


@pytest.mark.parametrize("during_grace", [False, True])
def test_auxiliary_tool_does_not_hide_trailing_delegate(during_grace):
    prefix = [_terminal(), _tool("get_weather", "aux")] if during_grace else [
        _tool("get_weather", "aux"), _terminal(),
    ]
    agent = _agent([*prefix, _tool()])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == ["get_weather", "delegate_to_main"]
    _assert_done(agent, events)


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("plain_model", [False, True])
def test_plain_model_or_disabled_grace_finishes_immediately(monkeypatch, generation, plain_model):
    model = "gemini-3.8-live" if plain_model else "gemini-3.8-live-extended-thinking"
    if not plain_model:
        monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0)
    agent = _agent([_terminal(generation=generation), _tool()], model=model)
    events = _receive(agent)
    _assert_done(agent, events)
    assert agent._session.reads == 1
    assert not _calls(events)
    assert events[-1].fallback_to_main is not plain_model
    assert events[-1].execution_completed is plain_model


@pytest.mark.parametrize("spoken", ["I can help with that.", "Sorry, there was a system error."])
@pytest.mark.parametrize("generation", [False, True])
def test_spoken_filler_or_error_without_outcome_falls_back_with_original_request(spoken, generation):
    response = _terminal(generation=generation)
    response.server_content.output_transcription = SimpleNamespace(text=spoken)
    agent = _agent([response])
    events = _receive(agent)
    assert any(isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)
               and event.output.text == spoken for event in events)
    done = events[-1]
    assert isinstance(done, TurnDoneEvent)
    assert done.fallback_to_main
    assert not done.execution_completed
    assert done.user_transcript == "play a song"
    _assert_done(agent, events)


@pytest.mark.parametrize("after_terminal", [False, True])
def test_complete_response_is_acknowledged_internally_and_confirms_direct_answer(after_terminal):
    response = _terminal()
    response.server_content.turn_complete = False
    response.server_content.output_transcription = SimpleNamespace(text="Two plus two is four.")
    completion = _tool("complete_response", "direct-answer")
    completion.tool_call.function_calls[0].args = {}
    suffix = [_terminal(), completion] if after_terminal else [completion, _terminal()]
    agent = _agent([response, *suffix])
    agent._user_transcript = "What is two plus two?"
    events = _receive(agent)
    assert not _calls(events)
    assert not events[-1].fallback_to_main
    assert events[-1].execution_completed
    assert not agent._pending_tool_calls
    response, = agent._session.tool_responses
    assert response.name == "complete_response"
    assert response.id == "direct-answer"
    assert response.response == {"result": "recorded"}
    assert response.scheduling is None
    _assert_done(agent, events)


def test_auxiliary_tool_without_outcome_preserves_request_for_fallback():
    agent = _agent([_tool("express_emotion", "emotion"), _terminal()])
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed
    assert events[-1].user_transcript == "play a song"


def test_delegate_after_complete_response_wins_and_keeps_original_request():
    completion = _tool("complete_response", "premature-completion")
    completion.tool_call.function_calls[0].args = {}
    agent = _agent([_terminal(), completion, _tool()])
    events = _receive(agent)

    calls = _calls(events)
    assert len(calls) == 1
    assert calls[0].name == "delegate_to_main"
    assert calls[0].user_transcript == "play a song"
    assert calls[0].user_turn_id == "user-1"
    assert isinstance(events[-1], TurnDoneEvent)
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)
    assert agent._session.messages.empty()
    response, = agent._session.tool_responses
    assert response.name == "complete_response"
    assert response.id == "premature-completion"


def test_completion_ack_does_not_repeat_spoken_answer_or_hide_later_delegate():
    def answer():
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text="Two plus two is four.")
        return message

    completion = _tool("complete_response", "direct-answer")
    completion.tool_call.function_calls[0].args = {}
    agent = _agent([answer(), _terminal(), completion, answer(), _tool()])
    events = _receive(agent)
    texts = [event.output.text for event in events
             if isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)]
    assert texts == ["Two plus two is four."]
    call, = _calls(events)
    assert call.name == "delegate_to_main"
    assert call.user_transcript == "play a song"
    assert isinstance(events[-1], TurnDoneEvent)
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)


@pytest.mark.parametrize('generation', [False, True])
@pytest.mark.parametrize('initial_result,final_result,delegate,terminal,release', [
    (False, True, False, True, True),
    (False, True, True, True, False),
    (False, False, False, True, False),
    (False, None, False, True, False),
    (True, True, False, True, False),
    (None, True, False, True, False),
    (False, True, False, False, False),
    (False, True, False, 'confirmation', True),
    (False, True, True, 'confirmation', False),
    (None, True, False, 'confirmation', False),
])
def test_continuation_after_filler_waits_for_outcome_and_late_routing(
        monkeypatch, generation, initial_result, final_result, delegate, terminal, release):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_NONBLOCKING_TOOL_GRACE_S', 0.08)
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_OUTCOME_TIMEOUT_S', 0.12)

    filler = 'Let me check that weather for you.'
    answer = 'Ho Chi Minh City is rainy today.'

    def speech(text):
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text=text)
        message.server_content.model_turn = SimpleNamespace(parts=[
            SimpleNamespace(thought=False, text=None,
                            inline_data=SimpleNamespace(data=b'\x00\x00' * 4)),
        ])
        return message

    messages = [speech(filler), _terminal(generation=generation), speech(answer)]
    if terminal == 'confirmation':
        messages.append(_tool('complete_response'))
    elif terminal:
        messages.append(_terminal(generation=generation))
    if delegate:
        messages.append(_tool())
    agent = _agent(messages)
    agent._user_transcript = 'What is the weather in Ho Chi Minh City?'
    observed = []

    async def check(request, spoken, **kwargs):
        # Even while the classifier runs, the candidate must stay off-speaker.
        queued = list(agent._recv_queue.queue)
        assert not any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                       and e.output.text == answer for e in queued)
        observed.append(spoken)
        return initial_result if spoken == filler else final_result

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    events = _receive(agent)
    texts = [e.output.text for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, TextOutput)]
    assert texts == ([filler, answer] if release else [filler])
    audio = [e for e in events if isinstance(e, OutputEvent) and isinstance(e.output, AudioOutput)]
    assert len(audio) == (2 if release else 1)
    assert bool(_calls(events)) is delegate
    assert events[-1].fallback_to_main is (not delegate and not release and initial_result is not True)
    assert events[-1].user_transcript == 'What is the weather in Ho Chi Minh City?'
    if release:
        assert observed == [filler, filler + answer]
    _assert_done(agent, events)


@pytest.mark.parametrize('delegate', [False, True])
@pytest.mark.parametrize('spoken', [False, True])
def test_handoff_quarantines_before_consumer_can_start_next_capture(
        monkeypatch, delegate, spoken):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return False

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    messages = []
    if spoken:
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text='Let me check.')
        messages.append(message)
    messages.append(_tool() if delegate else _terminal())
    if delegate:
        messages.append(_terminal())
    agent = _agent(messages)
    boundaries = []

    class ObservedQueue(queue.Queue):
        def put(self, event):
            if isinstance(event, TurnDoneEvent) or (
                    isinstance(event, OutputEvent) and isinstance(event.output, FunctionCallOutput)):
                boundaries.append(getattr(agent, '_requires_fresh_session', False))
            super().put(event)

    agent._recv_queue = ObservedQueue()
    _receive(agent)
    assert boundaries
    assert all(value is (spoken or delegate) for value in boundaries)


@pytest.mark.parametrize('model', ['gemini-3.8-live-extended-thinking', 'gemini-3.1-flash-live-preview'])
def test_silent_delegate_quarantine_survives_successful_tool_ack(model):
    from hal.realtime.models import FunctionCallResultInput
    agent = _agent([_tool(), _terminal()], model=model)
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == ['delegate_to_main']
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id='c1', output='{"result":"delegated"}',
    )))
    assert not agent._pending_tool_calls
    assert agent.requires_fresh_session
    assert agent._session.tool_responses[-1].name == 'delegate_to_main'


def test_empty_delegate_does_not_quarantine_session_for_handoff():
    tool = _tool()
    tool.tool_call.function_calls[0].args = {'message': ' '}
    agent = _agent([tool, _terminal()])
    _receive(agent)
    assert not getattr(agent, '_requires_fresh_session', False)


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("outcome", ["complete_response", "delegate_to_main", None])
def test_first_terminal_stops_extra_speech_and_audio_but_keeps_routing(monkeypatch, generation, outcome):
    from hal.realtime import response_outcome
    direct_answer = outcome == "complete_response"

    async def check(*args, **kwargs):
        return direct_answer

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    initial_text = "Two plus two is four." if direct_answer else "I can help with that."
    extra_text = "Sorry, there was a system error." if direct_answer else "I cannot access your email."
    request = "What is two plus two?" if direct_answer else "Read my newest email."

    def spoken(text):
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text=text)
        message.server_content.model_turn = SimpleNamespace(parts=[
            SimpleNamespace(thought=False, text=None,
                            inline_data=SimpleNamespace(data=b"\x00\x00" * 4)),
        ])
        return message

    messages = [spoken(initial_text), _terminal(generation=generation), spoken(extra_text)]
    if outcome is not None:
        tool = _tool(outcome)
        tool.tool_call.function_calls[0].args = {} if direct_answer else {"message": request}
        messages.append(tool)
    agent = _agent(messages)
    agent._user_transcript = request
    events = _receive(agent)
    outputs = [event.output for event in events if isinstance(event, OutputEvent)]
    assert [output.text for output in outputs if isinstance(output, TextOutput)] == [initial_text]
    assert len([output for output in outputs if isinstance(output, AudioOutput)]) == 1
    assert events[-1].fallback_to_main is (outcome is None)
    assert events[-1].execution_completed is (outcome is not None)
    assert events[-1].user_transcript == request
    if outcome == "delegate_to_main":
        call, = _calls(events)
        assert call.name == outcome
        assert call.user_transcript == request
    else:
        assert not _calls(events)
    _assert_done(agent, events)


@pytest.mark.parametrize("extended", [False, True])
def test_fail_fast_preserves_request_and_explicitly_falls_back_for_extended(extended):
    model = "gemini-3.8-live-extended-thinking" if extended else "gemini-3.1-flash-live-preview"
    agent = _agent([], model=model)
    agent._fail_fast_turn("test connection closed")
    done = agent._recv_queue.get_nowait()
    assert isinstance(done, TurnDoneEvent)
    assert done.fallback_to_main is extended
    assert not done.execution_completed
    assert done.user_transcript == "play a song"
    assert done.user_turn_id == "user-1"
    assert agent._turn_done.is_set()
    assert agent._recv_queue.empty()
    agent._fail_fast_turn("duplicate close notification")
    assert agent._recv_queue.empty()


def test_grace_timeout_leaves_session_usable_without_replaying_frames():
    async def scenario():
        agent = _agent([_terminal()])
        await asyncio.wait_for(agent._async_receive_turn(), timeout=0.5)
        first_done = agent._recv_queue.get_nowait()
        assert isinstance(first_done, TurnDoneEvent)
        assert first_done.user_turn_id == "user-1"
        assert agent._session.messages.empty()

        agent._live_user_turn_id = "user-2"
        agent._turn_done.clear()
        agent._session.messages.put_nowait(_terminal())
        agent._session.messages.put_nowait(_tool())
        await asyncio.wait_for(agent._async_receive_turn(), timeout=0.5)
        tool = agent._recv_queue.get_nowait()
        done = agent._recv_queue.get_nowait()
        assert isinstance(tool.output, FunctionCallOutput)
        assert tool.output.user_turn_id == "user-2"
        assert isinstance(done, TurnDoneEvent)
        assert done.user_turn_id == "user-2"
        assert agent._turn_done.is_set()
        assert agent._recv_queue.empty()
        assert agent._session.messages.empty()

    asyncio.run(scenario())


def test_filler_is_emitted_immediately_while_waiting_for_delayed_delegate(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 5.0)

    class WaitingSession(_Session):
        def __init__(self, messages):
            super().__init__(messages)
            self.waiting_for_tool = asyncio.Event()

        async def _receive(self):
            if self.messages.empty():
                self.waiting_for_tool.set()
            return await super()._receive()

    async def scenario():
        filler = _terminal()
        filler.server_content.turn_complete = False
        filler.server_content.output_transcription = SimpleNamespace(text="I can help with that.")
        agent = _agent([])
        agent._session = WaitingSession([filler, _terminal()])
        task = asyncio.create_task(agent._async_receive_turn())
        try:
            # Synchronize on the next socket read, rather than relying on a sleep:
            # the terminal has arrived and grace is actively waiting for a tool.
            await asyncio.wait_for(agent._session.waiting_for_tool.wait(), timeout=1.0)
            assert not task.done()
            assert not agent._turn_done.is_set()
            early_events = []
            while not agent._recv_queue.empty():
                early_events.append(agent._recv_queue.get_nowait())
            texts = [event.output.text for event in early_events
                     if isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)]
            assert texts == ["I can help with that."]
            assert not any(isinstance(event, TurnDoneEvent) for event in early_events)
            assert agent._user_transcript == "play a song"

            agent._session.messages.put_nowait(_tool())
            await asyncio.wait_for(task, timeout=1.0)
            call = agent._recv_queue.get_nowait()
            done = agent._recv_queue.get_nowait()
            assert isinstance(call.output, FunctionCallOutput)
            assert call.output.name == "delegate_to_main"
            assert call.output.user_transcript == "play a song"
            assert call.output.user_turn_id == "user-1"
            _assert_done(agent, [done])
            assert agent._recv_queue.empty()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('complete', [True, False])
@pytest.mark.parametrize('generation', [True, False])
def test_independent_outcome_preserves_spoken_turn_and_route(monkeypatch, complete, generation):
    from hal.realtime import response_outcome
    observed = []

    async def check(request, answer, **kwargs):
        observed.append((request, answer))
        return complete

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal(generation=generation)
    speech.server_content.output_transcription = SimpleNamespace(text='Cali is rainy. Otters hold hands.')
    agent = _agent([speech, _terminal()])
    agent._user_transcript = 'Tell me something fun and check the weather in Cali.'
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert observed == [('Tell me something fun and check the weather in Cali.',
                         'Cali is rainy. Otters hold hands.')]
    assert done.execution_completed is complete
    assert done.fallback_to_main is (not complete)
    assert done.user_turn_id == 'user-1'
    assert done.user_transcript == observed[0][0]
    texts = [e.output.text for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, TextOutput)]
    assert texts == [observed[0][1]]
    assert not agent._session.control_turns


def test_late_delegate_wins_over_independent_completion(monkeypatch):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return True

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='Here is a fun fact.')
    agent = _agent([speech, _tool()])
    events = _receive(agent)
    calls = [e.output.name for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, FunctionCallOutput)]
    assert calls == ['delegate_to_main']


@pytest.mark.parametrize('generation', [True, False])
@pytest.mark.parametrize('verdict,needs_main', [
    ('CLARIFICATION', False), ('INCOMPLETE', True), ('unexpected', True),
])
def test_weather_clarification_routes_through_real_outcome_parser(
        monkeypatch, generation, verdict, needs_main):
    import anthropic
    import json
    from hal.realtime import response_outcome

    request = 'Can you tell me the weather today and Bitcoin pricing?'
    answer = ("Bitcoin is trading at around 86,400 USD today. "
              "To give you the correct weather, I'll need to know which city you're in.")
    captured = []

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def text_stream(self):
            async def chunks():
                yield verdict
            return chunks()

    class Client(Stream):
        def __init__(self, **kwargs):
            self.messages = self

        def stream(self, **kwargs):
            captured.append(json.loads(kwargs['messages'][0]['content']))
            return Stream()

    # Mock only the text-model transport, preserving classification parsing and
    # Gemini terminal routing together. No live API or device action is needed.
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', Client)
    monkeypatch.setattr(response_outcome.app_config, 'REALTIME_SUMMARIZER_API_KEY', 'test')
    speech = _terminal(generation=generation)
    speech.server_content.output_transcription = SimpleNamespace(text=answer)
    agent = _agent([speech, _terminal()])
    agent._user_transcript = request
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert captured == [{'request': request, 'spoken_answer': answer,
                         'public_search_evidence': False}]
    assert done.fallback_to_main is needs_main
    assert done.execution_completed is (not needs_main)
    assert done.user_transcript == request
    assert not _calls(events)
    _assert_done(agent, events)


def test_filler_confirmation_cannot_override_incomplete_check(monkeypatch):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return False

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='I can help with that.')
    agent = _agent([speech, _tool('complete_response')])
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert done.fallback_to_main
    assert not done.execution_completed
    assert done.user_transcript == 'play a song'


def test_slow_outcome_check_is_cancelled_at_its_own_deadline(monkeypatch):
    from hal.realtime import response_outcome
    cancelled = []

    async def check(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='I can help with that.')
    agent = _agent([speech])
    events = _receive(agent)
    assert cancelled == [True]
    assert next(e for e in events if isinstance(e, TurnDoneEvent)).fallback_to_main


@pytest.mark.parametrize("generation", [True, False])
def test_empty_terminal_then_real_answer_gets_fresh_outcome_window(monkeypatch, generation):
    from hal.realtime import response_outcome
    observed = []

    async def check(request, answer, **kwargs):
        observed.append((answer, kwargs["timeout"]))
        return True

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)
    response = _terminal(generation=generation)
    response.server_content.output_transcription = SimpleNamespace(text="Two plus two is four.")
    agent = _agent([_terminal(generation=generation), response])
    events = _receive(agent)
    assert observed[0][0] == "Two plus two is four."
    assert observed[0][1] > 0
    assert [e.output.text for e in events if isinstance(e, OutputEvent)
            and isinstance(e.output, TextOutput)] == ["Two plus two is four."]
    assert not events[-1].fallback_to_main


def _grounding():
    message = _terminal()
    message.server_content.turn_complete = False
    message.server_content.grounding_metadata = SimpleNamespace(
        web_search_queries=["AI news"],
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(
            title="News", uri="https://example.com/news", snippet="New AI model"))],
    )
    return message


def test_search_progress_waits_for_answer_beyond_normal_grace(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.15)

    async def check(*args, **kwargs):
        return True

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)

    async def scenario():
        agent = _agent([_terminal(generation=True), _grounding()])
        agent._committed_at = time.monotonic()
        task = asyncio.create_task(agent._async_receive_turn())
        await asyncio.sleep(0.05)
        assert not task.done()
        reply = _terminal(generation=True)
        reply.server_content.output_transcription = SimpleNamespace(text="Here is the AI news.")
        agent._session.messages.put_nowait(reply)
        await asyncio.wait_for(task, timeout=0.08)
        events = list(agent._recv_queue.queue)
        assert not events[-1].fallback_to_main
        assert any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                   and e.output.text == "Here is the AI news." for e in events)

    asyncio.run(scenario())


def test_repeated_grounding_does_not_reset_absolute_progress_deadline(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.07)

    async def scenario():
        agent = _agent([_terminal(), _grounding()])
        agent._committed_at = time.monotonic()
        task = asyncio.create_task(agent._async_receive_turn())
        await asyncio.sleep(0.04)
        agent._session.messages.put_nowait(_grounding())
        await asyncio.wait_for(task, timeout=0.055)
        done = agent._recv_queue.get_nowait()
        assert done.fallback_to_main
        assert done.user_transcript == "play a song"
        assert "AI news" in done.handoff_context
        assert "https://example.com/news" in done.handoff_context
        assert len(done.handoff_context) <= 6000

    asyncio.run(scenario())


def test_active_continuation_is_not_cut_at_initial_grace(monkeypatch):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return True

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)

    async def scenario():
        agent = _agent([_terminal(generation=True)])
        task = asyncio.create_task(agent._async_receive_turn())
        for text in ["Two ", "plus two ", "is four."]:
            await asyncio.sleep(0.012)
            response = _terminal()
            response.server_content.turn_complete = False
            response.server_content.output_transcription = SimpleNamespace(text=text)
            agent._session.messages.put_nowait(response)
        agent._session.messages.put_nowait(_terminal(generation=True))
        await asyncio.wait_for(task, timeout=0.1)
        events = list(agent._recv_queue.queue)
        assert not events[-1].fallback_to_main
        assert "".join(e.output.text for e in events if isinstance(e, OutputEvent)
                       and isinstance(e.output, TextOutput)) == "Two plus two is four."

    asyncio.run(scenario())


def test_search_without_terminal_is_bounded_and_quarantined(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.035)
    agent = _agent([_grounding()])
    agent._committed_at = time.monotonic()
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert "AI news" in events[-1].handoff_context
    assert agent._requires_fresh_session


def test_zero_progress_budget_disables_extension(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0)
    agent = _agent([_grounding(), _terminal()])
    agent._committed_at = time.monotonic()
    events = _receive(agent)
    assert getattr(agent, "_progress_deadline_at", 0.0) == 0.0
    assert events[-1].fallback_to_main


def test_grounded_fallback_does_not_wait_past_budget_for_slow_outcome(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.035)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_OUTCOME_TIMEOUT_S", 0.3)
    cancelled = []

    async def check(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)

    async def scenario():
        filler = _terminal()
        filler.server_content.output_transcription = SimpleNamespace(text="Let me check.")
        agent = _agent([_grounding(), filler])
        agent._committed_at = time.monotonic()
        await asyncio.wait_for(agent._async_receive_turn(), timeout=0.09)
        assert cancelled == [True]
        assert list(agent._recv_queue.queue)[-1].fallback_to_main

    asyncio.run(scenario())

def test_late_first_terminal_does_not_extend_search_budget(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.06)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0.08)

    async def scenario():
        agent = _agent([_grounding()])
        agent._committed_at = time.monotonic()
        task = asyncio.create_task(agent._async_receive_turn())
        await asyncio.sleep(0.035)
        agent._session.messages.put_nowait(_terminal())
        await asyncio.wait_for(task, timeout=0.055)
        assert list(agent._recv_queue.queue)[-1].fallback_to_main

    asyncio.run(scenario())


def test_terminal_answer_before_progress_cap_cannot_add_classifier_window(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.06)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_RECV_QUEUE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_OUTCOME_TIMEOUT_S", 0.3)

    async def check(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)

    async def scenario():
        agent = _agent([_terminal(generation=True), _grounding()])
        agent._committed_at = time.monotonic()
        task = asyncio.create_task(agent._async_receive_turn())
        await asyncio.sleep(0.035)
        response = _terminal(generation=True)
        response.server_content.output_transcription = SimpleNamespace(text="News result.")
        agent._session.messages.put_nowait(response)
        await asyncio.wait_for(task, timeout=0.055)
        assert list(agent._recv_queue.queue)[-1].fallback_to_main

    asyncio.run(scenario())


def test_actual_answer_streaming_past_search_cap_can_finish(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_PROGRESS_TIMEOUT_S", 0.03)

    async def check(*args, **kwargs):
        await asyncio.sleep(0.005)
        return True

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)

    async def scenario():
        agent = _agent([_terminal(generation=True), _grounding()])
        agent._committed_at = time.monotonic()
        task = asyncio.create_task(agent._async_receive_turn())
        for text in ["The ", "news ", "today."]:
            await asyncio.sleep(0.015)
            chunk = _terminal()
            chunk.server_content.turn_complete = False
            chunk.server_content.output_transcription = SimpleNamespace(text=text)
            agent._session.messages.put_nowait(chunk)
        agent._session.messages.put_nowait(_terminal(generation=True))
        await asyncio.wait_for(task, timeout=0.09)
        assert not list(agent._recv_queue.queue)[-1].fallback_to_main

    asyncio.run(scenario())


def _speech(text):
    message = _terminal(generation=True)
    message.server_content.generation_complete = False
    message.server_content.output_transcription = SimpleNamespace(text=text)
    return message


async def _commit_look_replay(agent):
    async def send_realtime_input(**kwargs):
        pass

    agent._session.send_realtime_input = send_realtime_input
    agent._vad_disabled = agent._activity_started = True
    agent.flush_output()
    agent.skip_next_turn_done()
    agent._committed_at = time.monotonic()
    await agent._async_commit()


@pytest.mark.parametrize('boundary', ['none', 'interrupt', 'terminal', 'generation', 'both', 'separate'])
@pytest.mark.parametrize('cancel', [False, True])
def test_committed_look_replay_starts_new_response(monkeypatch, boundary, cancel):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', False)
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_PROGRESS_TIMEOUT_S', 0.15)
    observed = []

    async def check(request, answer, **kwargs):
        observed.append((request, answer))
        return answer != 'Let me take a look.'

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)

    async def scenario():
        agent = _agent([_speech('Let me take a look.'), _terminal(generation=True)])
        agent._user_transcript = 'What am I holding?'
        task = asyncio.create_task(agent._async_receive_turn())
        while agent._session.reads < 3:
            await asyncio.sleep(0)
        await _commit_look_replay(agent)
        if boundary != 'none':
            message = _terminal()
            message.server_content.interrupted = boundary in {'interrupt', 'both', 'separate'}
            message.server_content.turn_complete = boundary in {'terminal', 'both'}
            message.server_content.generation_complete = boundary == 'generation'
            agent._session.messages.put_nowait(message)
            if boundary == 'separate':
                agent._session.messages.put_nowait(_terminal())
        # Cross the original 20ms filler grace. Replay must own a fresh budget.
        await asyncio.sleep(0.035)
        assert not task.done()
        assert not agent._skip_stale_turn_done
        agent._session.messages.put_nowait(_speech('You are holding a phone.'))
        if cancel:
            message = _terminal(generation=True)
            message.server_content.generation_complete = False
            message.server_content.interrupted = True
            agent._session.messages.put_nowait(message)
        agent._session.messages.put_nowait(_terminal(generation=True))
        await asyncio.wait_for(task, timeout=0.3)
        events = list(agent._recv_queue.queue)
        texts = [e.output.text for e in events
                 if isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)]
        assert texts == ([] if cancel else ['You are holding a phone.'])
        assert events[-1].execution_completed is (not cancel)
        assert not events[-1].fallback_to_main
        if not cancel:
            assert observed[-1] == ('What am I holding?', 'You are holding a phone.')
        assert not [t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()]

    asyncio.run(scenario())


@pytest.mark.parametrize('live', [False, True])
def test_ordinary_interrupt_still_cancels(monkeypatch, live):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', live)
    message = _terminal(generation=True)
    message.server_content.generation_complete = False
    message.server_content.interrupted = True
    agent = _agent([_terminal(generation=True), message,
                    _speech('This should not be released.'), _terminal(generation=True)])
    events = _receive(agent)
    assert not any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                   for e in events)
    assert not events[-1].execution_completed


def test_silent_replay_fallback_is_not_marked_as_stale(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', False)
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_PROGRESS_TIMEOUT_S', 0.05)

    async def scenario():
        agent = _agent([_terminal(generation=True)])
        task = asyncio.create_task(agent._async_receive_turn())
        while agent._session.reads < 2:
            await asyncio.sleep(0)
        await _commit_look_replay(agent)
        await asyncio.wait_for(task, timeout=0.15)
        events = list(agent._recv_queue.queue)
        assert len(events) == 1
        assert events[0].fallback_to_main
        assert not agent._skip_stale_turn_done
        assert not [t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()]

    asyncio.run(scenario())


def test_replay_retires_pending_filler_classifier(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', False)
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_PROGRESS_TIMEOUT_S', 0.15)
    retired = []

    async def check(request, answer, **kwargs):
        if answer == 'Let me take a look.':
            try:
                await asyncio.Future()
            finally:
                retired.append(answer)
        return True

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)

    async def scenario():
        agent = _agent([_speech('Let me take a look.'), _terminal(generation=True)])
        task = asyncio.create_task(agent._async_receive_turn())
        while agent._session.reads < 3:
            await asyncio.sleep(0)
        await _commit_look_replay(agent)
        agent._session.messages.put_nowait(_speech('You are holding a phone.'))
        agent._session.messages.put_nowait(_terminal(generation=True))
        await asyncio.wait_for(task, timeout=0.3)
        assert retired == ['Let me take a look.']
        assert list(agent._recv_queue.queue)[-1].execution_completed

    asyncio.run(scenario())


def test_cancel_during_replay_reset_closes_pending_socket_read(monkeypatch):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', False)

    async def scenario():
        checking = asyncio.Event()
        cleaning = asyncio.Event()

        async def check(*args, **kwargs):
            checking.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await asyncio.Future()

        monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
        agent = _agent([_speech('Let me take a look.'), _terminal(generation=True)])
        task = asyncio.create_task(agent._async_receive_turn())
        await checking.wait()
        await _commit_look_replay(agent)
        await cleaning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not [t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()]

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.5))


@pytest.mark.parametrize('live,replay', [(True, True), (False, False)])
def test_ordinary_or_live_commit_does_not_signal_replay(monkeypatch, live, replay):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', live)

    async def scenario():
        agent = _agent([])
        agent._replay_commit_signal = asyncio.Event()
        async def send_realtime_input(**kwargs):
            pass
        agent._session.send_realtime_input = send_realtime_input
        agent._vad_disabled = agent._activity_started = True
        if replay:
            agent.skip_next_turn_done()
        await agent._async_commit()
        assert not agent._replay_commit_signal.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("after_terminal", [False, True])
@pytest.mark.parametrize("late_delegate", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_early_completion_does_not_mute_visual_answer(monkeypatch, after_terminal, late_delegate, live):
    monkeypatch.setattr(gemini_live.app_config, "LIVE_MODE", live)
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return True

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)
    answer = _terminal()
    answer.server_content.turn_complete = False
    answer.server_content.output_transcription = SimpleNamespace(
        text="You're wearing a beige shirt with DO IT ANYWAY printed on it.")
    messages = [_tool("complete_response", "early")]
    if after_terminal:
        messages.append(_terminal())
    messages.extend([answer, _terminal()])
    if late_delegate:
        messages.append(_tool())
    agent = _agent(messages)
    agent._user_transcript = "Look at me and see what I wear."
    events = _receive(agent)
    texts = [e.output.text for e in events
             if isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)]
    assert bool(texts) == (not after_terminal or not late_delegate)
    if texts:
        assert texts == [answer.server_content.output_transcription.text]
    assert not events[-1].fallback_to_main
    assert bool(_calls(events)) == late_delegate


def test_early_completion_without_answer_still_falls_back():
    agent = _agent([_tool("complete_response", "empty"), _terminal()])
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed
    assert not agent._pending_tool_calls


@pytest.mark.parametrize("live", [False, True])
def test_interrupt_after_early_completion_still_cancels(monkeypatch, live):
    monkeypatch.setattr(gemini_live.app_config, "LIVE_MODE", live)
    interrupt = _terminal(generation=True)
    interrupt.server_content.generation_complete = False
    interrupt.server_content.interrupted = True
    agent = _agent([
        _tool("complete_response", "early"), _terminal(generation=True),
        interrupt, _speech("This should not be released."), _terminal(generation=True),
    ])
    events = _receive(agent)
    assert not any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                   for e in events)
    assert not events[-1].execution_completed


def _status(value):
    message = _terminal()
    message.server_content.interaction_status = value
    return message


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("completed", [True, False])
def test_server_status_keeps_filler_and_answer_in_one_interaction(monkeypatch, live, completed):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "LIVE_MODE", live)
    checked = []

    async def check(request, answer, **kwargs):
        checked.append(answer)
        return completed

    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)
    agent = _agent([
        _speech("Let me look."), _terminal(generation=True), _status("IN_PROGRESS"),
        _tool("complete_response"), _speech("Your shirt says DO IT ANYWAY."),
        _terminal(generation=True), _status("IDLE"),
    ])
    events = _receive(agent)
    texts = [e.output.text for e in events
             if isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)]
    assert texts == ["Let me look.", "Your shirt says DO IT ANYWAY."]
    # A provisional pre-status check may run, but never a second check of the
    # completed interaction. Its result must not override provider IDLE.
    assert all(answer == "Let me look." for answer in checked)
    assert not events[-1].fallback_to_main
    assert events[-1].execution_completed


def test_routing_finishes_in_progress_without_waiting_for_idle():
    agent = _agent([_status("IN_PROGRESS"), _tool()])
    events = _receive(agent)
    assert _calls(events)[0].name == "delegate_to_main"
    assert not events[-1].fallback_to_main


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("same_frame", [False, True])
def test_idle_answer_finishes_without_waiting_for_unavailable_checker(monkeypatch, live, same_frame):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, "LIVE_MODE", live)
    checked = []

    async def stalled_check(*args, **kwargs):
        checked.append(args)
        await asyncio.Future()

    monkeypatch.setattr(response_outcome, "spoken_response_complete", stalled_check)
    answer = _speech("You are wearing a yellow shirt.")
    if same_frame:
        answer.server_content.interaction_status = "IDLE"
        answer.server_content.turn_complete = True
    messages = [_status("IN_PROGRESS"), _speech("Let me look."),
                _tool("complete_response"), answer]
    if not same_frame:
        messages.append(_status("IDLE"))
    agent = _agent(messages)
    agent._user_transcript = "Look at me. What am I wearing?"
    events = _receive(agent)
    _assert_done(agent, events)
    assert not checked
    assert not events[-1].fallback_to_main
    assert events[-1].execution_completed
    assert [e.output.text for e in events if isinstance(e, OutputEvent)
            and isinstance(e.output, TextOutput)] == [
                "Let me look.", "You are wearing a yellow shirt."]


@pytest.mark.parametrize("pending", [False, True])
def test_idle_does_not_claim_completion_for_empty_or_pending_work(pending):
    messages = [_status("IN_PROGRESS")]
    if pending:
        messages.extend([_speech("Let me look."), _tool("look", "look-pending")])
    messages.append(_status("IDLE"))
    agent = _agent(messages)
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed


@pytest.mark.parametrize("name", ["delegate_to_main", "reject_turn", "end_conversation"])
def test_idle_after_speech_preserves_explicit_routing(name):
    routing = _tool(name)
    routing.server_content = _status("IDLE").server_content
    agent = _agent([_status("IN_PROGRESS"), _speech("I'll pass that on."), routing])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == [name]
    assert not events[-1].fallback_to_main


def test_idle_with_interrupt_never_completes_or_falls_back():
    terminal = _status("IDLE")
    terminal.server_content.interrupted = True
    agent = _agent([_status("IN_PROGRESS"), _speech("Your shirt is yellow."), terminal])
    events = _receive(agent)
    assert not events[-1].execution_completed
    assert not events[-1].fallback_to_main


@pytest.mark.parametrize("live", [False, True])
def test_server_status_does_not_override_user_interrupt(monkeypatch, live):
    monkeypatch.setattr(gemini_live.app_config, "LIVE_MODE", live)
    interrupted = _terminal()
    interrupted.server_content.interrupted = True
    agent = _agent([_status("IN_PROGRESS"), interrupted, _speech("Stale answer")])
    agent._pending_image = object()
    events = _receive(agent)
    assert not events[-1].execution_completed
    assert agent._pending_image is None
    assert not any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput) for e in events)


def test_in_progress_stall_has_bounded_fallback(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_TURN_MAX_SILENCE_S", 0.02)
    agent = _agent([_status("IN_PROGRESS")])
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert agent.requires_fresh_session


@pytest.mark.parametrize("status", ["IN_PROGRESS", "IDLE"])
def test_status_with_routing_tool_does_not_lose_handoff(status):
    message = _tool()
    message.server_content = _status(status).server_content
    agent = _agent([message])
    events = _receive(agent)
    assert _calls(events)[0].name == "delegate_to_main"
    assert not events[-1].fallback_to_main


def test_first_status_preserves_already_buffered_answer_prefix(monkeypatch):
    from hal.realtime import response_outcome
    async def check(*args, **kwargs):
        return True
    monkeypatch.setattr(response_outcome, "spoken_response_complete", check)
    agent = _agent([
        _speech("Let me see."), _terminal(generation=True),
        _speech("Your shirt "), _status("IN_PROGRESS"),
        _speech("is yellow."), _status("IDLE"),
    ])
    events = _receive(agent)
    texts = [e.output.text for e in events
             if isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)]
    assert texts == ["Let me see.", "Your shirt ", "is yellow."]
    assert not events[-1].fallback_to_main


@pytest.mark.parametrize("interrupt", [False, True])
def test_receiver_invalidates_cancelled_look_image(interrupt):
    cancelled = _terminal()
    cancelled.server_content.turn_complete = False
    if interrupt:
        cancelled.server_content.interrupted = True
    else:
        cancelled.tool_call_cancellation = SimpleNamespace(ids=["look-1"])
    agent = _agent([_status("IN_PROGRESS"), cancelled, _status("IDLE")])
    agent._pending_tool_calls = {"look-1", "emotion-1"}
    agent._pending_tool_names = {"look-1": "look", "emotion-1": "express_emotion"}
    _receive(agent)
    assert agent._cancelled_look_calls == {"look-1"}
    assert agent.requires_fresh_session


@pytest.mark.parametrize('prior_speech', ['', 'Rất tiếc, đã xảy ra một lỗi hệ thống.', 'Arbitrary model reply.'])
@pytest.mark.parametrize('fresh_kind', ['transcript', 'activity_start'])
@pytest.mark.parametrize('orphan_text', ['Unrelated acknowledgement.', 'Một câu bất kỳ.', '通知'])
def test_live_reject_barrier_survives_ack_and_receive_restart(monkeypatch, orphan_text, fresh_kind, prior_speech):
    from hal.realtime.models import FunctionCallResultInput, InterruptedOutput
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    agent = _agent([*([_speech(prior_speech)] if prior_speech else []),
                    _tool('reject_turn'), _terminal()])
    first = _receive(agent)
    assert all(not event.execution_completed and not event.fallback_to_main
               for event in first if isinstance(event, TurnDoneEvent))
    assert [call.name for call in _calls(first)] == ['reject_turn']
    assert agent._reject_followup_barrier
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id='c1', output='{"result":"turn dropped"}',
    )))
    assert not agent._pending_tool_calls
    assert agent._reject_followup_barrier

    orphan = _terminal()
    orphan.server_content.turn_complete = False
    orphan.server_content.output_transcription = SimpleNamespace(text=orphan_text)
    orphan.server_content.model_turn = SimpleNamespace(parts=[
        SimpleNamespace(inline_data=SimpleNamespace(data=b'\x00\x01' * 160))])
    agent._session.messages.put_nowait(orphan)
    empty_finished = _terminal()
    empty_finished.server_content.turn_complete = False
    empty_finished.server_content.input_transcription = SimpleNamespace(text='', finished=True)
    agent._session.messages.put_nowait(empty_finished)
    old_end = _terminal()
    old_end.server_content.turn_complete = False
    old_end.voice_activity = SimpleNamespace(voice_activity_type='ACTIVITY_END')
    agent._session.messages.put_nowait(old_end)
    agent._session.messages.put_nowait(_terminal())
    second = _receive(agent)
    assert agent._reject_followup_barrier
    assert not any(isinstance(event, OutputEvent) and isinstance(
        event.output, (AudioOutput, TextOutput, InterruptedOutput, FunctionCallOutput)) for event in second)
    assert all(not event.fallback_to_main and not event.execution_completed
               for event in second if isinstance(event, TurnDoneEvent))

    fresh = _terminal()
    fresh.server_content.turn_complete = False
    if fresh_kind == 'transcript':
        fresh.server_content.input_transcription = SimpleNamespace(text='What time is it?', finished=True)
    else:
        fresh.voice_activity = SimpleNamespace(voice_activity_type='ACTIVITY_START')
    agent._session.messages.put_nowait(fresh)
    reply = _terminal()
    reply.server_content.output_transcription = SimpleNamespace(text='A valid new answer.')
    agent._session.messages.put_nowait(reply)
    agent._session.messages.put_nowait(_tool('complete_response', 'new-call'))
    third = _receive(agent)
    assert not agent._reject_followup_barrier
    texts = [event.output for event in third if isinstance(event, OutputEvent)
             and isinstance(event.output, TextOutput)]
    assert [out.text for out in texts] == ['A valid new answer.']
    if fresh_kind == 'transcript':
        assert texts[0].user_turn_id and texts[0].user_turn_id != 'user-1'


def test_manual_rejection_does_not_arm_live_barrier(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', False)
    agent = _agent([_tool('reject_turn'), _terminal()])
    _receive(agent)
    assert not getattr(agent, '_reject_followup_barrier', False)


def test_live_reject_barrier_accepts_finished_only_transcription(monkeypatch):
    from google.genai import types

    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    # A real SDK completion marker has text=None, not an empty string.
    finished = types.LiveServerMessage(server_content=types.LiveServerContent(
        input_transcription=types.Transcription(finished=True),
    ))
    agent = _agent([finished, _terminal()])
    agent._reject_followup_barrier = True

    events = _receive(agent)

    assert agent._reject_followup_barrier
    assert not any(isinstance(event, OutputEvent) for event in events)
    assert len(events) == 1
    assert isinstance(events[0], TurnDoneEvent)
    assert not events[0].execution_completed
    assert not events[0].fallback_to_main


def test_live_reject_barrier_keeps_transport_controls(monkeypatch):
    from websockets.exceptions import ConnectionClosed
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    control = _terminal()
    control.server_content.turn_complete = False
    control.session_resumption_update = SimpleNamespace(new_handle='resume-new')
    control.tool_call_cancellation = SimpleNamespace(ids=['cancel-old'])
    agent = _agent([control, _terminal()])
    agent._reject_followup_barrier = True
    cancelled = []
    agent._invalidate_look_images = lambda ids: cancelled.extend(ids)
    _receive(agent)
    assert agent._resumption_handle == 'resume-new'
    assert cancelled == ['cancel-old']
    assert agent._reject_followup_barrier
    goodbye = _terminal()
    goodbye.go_away = SimpleNamespace(time_left=0)
    agent._session.messages.put_nowait(goodbye)
    with pytest.raises(ConnectionClosed):
        _receive(agent)


@pytest.mark.parametrize('routing', ['reject_turn', 'delegate_to_main'])
@pytest.mark.parametrize('text', ['Rất tiếc, đã xảy ra một lỗi hệ thống.', 'Any model-generated text.'])
def test_live_empty_terminal_then_progress_holds_speech_until_routing(monkeypatch, routing, text):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    agent = _agent([_terminal(generation=True), _status('IN_PROGRESS'),
                    _speech(text), _tool(routing)])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == [routing]
    assert not any(isinstance(event, OutputEvent) and isinstance(
        event.output, (TextOutput, AudioOutput)) for event in events)
    if routing == 'reject_turn':
        assert all(not event.execution_completed for event in events
                   if isinstance(event, TurnDoneEvent))


def test_live_empty_terminal_valid_answer_releases_only_at_idle(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    agent = _agent([_terminal(generation=True), _status('IN_PROGRESS'), _speech('Valid answer.')])

    async def scenario():
        task = asyncio.create_task(agent._async_receive_turn())
        # Wait for all three frames to be consumed, then observe the actual queue.
        while agent._session.reads < 4:
            await asyncio.sleep(0)
        assert agent._recv_queue.empty()
        agent._session.messages.put_nowait(_status('IDLE'))
        await asyncio.wait_for(task, 0.5)

    asyncio.run(asyncio.wait_for(scenario(), 1.0))
    events = list(agent._recv_queue.queue)
    assert [e.output.text for e in events if isinstance(e, OutputEvent)
            and isinstance(e.output, TextOutput)] == ['Valid answer.']
    assert all(e.execution_completed and not e.fallback_to_main for e in events
               if isinstance(e, TurnDoneEvent))


def test_live_initial_answer_still_streams_before_idle(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    agent = _agent([_status('IN_PROGRESS'), _speech('Initial answer.')])

    async def scenario():
        task = asyncio.create_task(agent._async_receive_turn())
        while agent._session.reads < 3:
            await asyncio.sleep(0)
        assert any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                   for e in agent._recv_queue.queue)
        agent._session.messages.put_nowait(_status('IDLE'))
        await asyncio.wait_for(task, 0.5)

    asyncio.run(asyncio.wait_for(scenario(), 1.0))


@pytest.mark.parametrize('ending', ['interrupt', 'overflow'])
def test_live_empty_terminal_does_not_release_cancelled_or_overflowed_speech(monkeypatch, ending):
    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    text = 'x' * 16_001 if ending == 'overflow' else 'Unfinished answer.'
    end = _status('IDLE')
    if ending == 'interrupt':
        end.server_content.interrupted = True
    agent = _agent([_terminal(generation=True), _status('IN_PROGRESS'), _speech(text), end])
    events = _receive(agent)
    assert not any(isinstance(e, OutputEvent) and isinstance(e.output, (TextOutput, AudioOutput))
                   for e in events)
    assert all(not e.execution_completed for e in events if isinstance(e, TurnDoneEvent))


@pytest.mark.parametrize('empty_terminal', [False, True])
@pytest.mark.parametrize('terminal', ['none', 'turn', 'generation'])
def test_live_same_frame_reject_never_publishes_speech(monkeypatch, empty_terminal, terminal):
    from hal.realtime.models import FunctionCallResultInput

    monkeypatch.setattr(gemini_live.app_config, 'LIVE_MODE', True)
    rejected = _speech('Arbitrary rejected speech.')
    rejected.server_content.model_turn = SimpleNamespace(parts=[
        SimpleNamespace(inline_data=SimpleNamespace(data=b'\x00\x01' * 160))])
    rejected.tool_call = _tool('reject_turn').tool_call
    rejected.server_content.turn_complete = terminal == 'turn'
    rejected.server_content.generation_complete = terminal == 'generation'
    prefix = [_terminal(generation=True), _status('IN_PROGRESS')] if empty_terminal else []
    suffix = [_terminal()] if terminal == 'none' and not empty_terminal else []
    agent = _agent([*prefix, rejected, *suffix])
    events = _receive(agent)
    assert [c.name for c in _calls(events)] == ['reject_turn']
    assert not any(isinstance(e, OutputEvent) and isinstance(e.output, (TextOutput, AudioOutput))
                   for e in events)
    assert all(not e.execution_completed and not e.fallback_to_main for e in events
               if isinstance(e, TurnDoneEvent))
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id='c1', output='{"result":"turn dropped"}')))
    fresh = _speech('')
    fresh.server_content.input_transcription = SimpleNamespace(text='New request.', finished=True)
    agent._session.messages.put_nowait(fresh)
    agent._session.messages.put_nowait(_status('IN_PROGRESS'))
    agent._session.messages.put_nowait(_speech('Only the new answer.'))
    agent._session.messages.put_nowait(_status('IDLE'))
    next_events = _receive(agent)
    assert [e.output.text for e in next_events if isinstance(e, OutputEvent)
            and isinstance(e.output, TextOutput)] == ['Only the new answer.']
    assert all(e.output.user_turn_id != 'user-1' for e in next_events
               if isinstance(e, OutputEvent) and isinstance(e.output, TextOutput))
