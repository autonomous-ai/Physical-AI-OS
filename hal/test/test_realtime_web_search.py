"""The client-side `web_search` tool for the pipecat provider.

Pins three things: the Google-Search relay's Interaction shape (captured from
a live call on 2026-09-21, trimmed), the orchestrator's tool contract (every
outcome is acknowledged with trigger_response=True and the turn continues), and
the registration gate (pipecat_v1 + flag only).
"""

import json
from unittest import mock

import pytest
import requests

import hal.config as hal_config
from hal.realtime import orchestrator as orch_mod
from hal.realtime import web_search
from hal.realtime.models import FunctionCallOutput, TextOutput
from hal.realtime.orchestrator import (
    WEB_SEARCH_TOOL,
    WEB_SEARCH_TOOL_NAME,
    RealtimeOrchestrator,
)
from hal.realtime.web_search import (
    SearchResult,
    WebSearchError,
    grounded_search,
    parse_interaction,
    strip_markdown,
)

# The relay's answer to "Who won the euro 2024?" — signatures, usage and the
# search-suggestion HTML dropped, everything the parser reads kept verbatim.
INTERACTION = {
    "id": "v1_abc",
    "status": "completed",
    "object": "interaction",
    "model": "gemini-3.7-flash",
    "steps": [
        {
            "id": "call_69444",
            "type": "google_search_call",
            "arguments": {"queries": ["who won euro 2024"]},
            "search_type": "web_search",
        },
        {
            "call_id": "call_69444",
            "type": "google_search_result",
            "result": [{"search_suggestions": "<style>…</style>"}],
            "is_error": False,
        },
        {"type": "thought"},
        {
            "type": "model_output",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "**Spain** won UEFA Euro 2024. \n\nThey defeated **England 2–1** "
                        "in the final on July 14, 2024, at the Olympiastadion in Berlin."
                    ),
                    "annotations": [
                        {"start_index": 0, "end_index": 28, "url": "https://x", "title": "wikipedia.org", "type": "url_citation"},
                        {"start_index": 32, "end_index": 135, "url": "https://y", "title": "youtube.com", "type": "url_citation"},
                        {"start_index": 32, "end_index": 135, "url": "https://z", "title": "wikipedia.org", "type": "url_citation"},
                    ],
                }
            ],
        },
    ],
}


# --- response parsing -----------------------------------------------------------------


def test_parse_reads_the_grounded_answer_queries_and_distinct_sources():
    res = parse_interaction(INTERACTION)
    assert res.answer.startswith("Spain won UEFA Euro 2024.")
    assert "England 2–1" in res.answer
    assert "**" not in res.answer, "markdown must not reach the voice model"
    assert res.queries == ["who won euro 2024"]
    assert res.sources == ["wikipedia.org", "youtube.com"]


def test_parse_accepts_the_canonical_outputs_key_too():
    payload = dict(INTERACTION)
    payload["outputs"] = payload.pop("steps")
    assert parse_interaction(payload).answer.startswith("Spain")


def test_parse_rejects_an_unfinished_or_empty_interaction():
    with pytest.raises(WebSearchError, match="status"):
        parse_interaction({"status": "failed", "steps": []})
    with pytest.raises(WebSearchError, match="no answer"):
        parse_interaction({"status": "completed", "steps": [{"type": "thought"}]})


def test_parse_caps_a_long_answer_at_a_sentence_boundary(monkeypatch):
    """The answer is spoken, not read — a grounded essay only costs tokens."""
    monkeypatch.setattr(web_search, "MAX_ANSWER_CHARS", 60)
    long_text = "First sentence here. Second sentence follows it. Third one is cut."
    payload = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": long_text}]}],
    }
    assert parse_interaction(payload).answer == "First sentence here. Second sentence follows it."


def test_strip_markdown_flattens_what_gemini_writes():
    text = "## Weather\n- **31°C** and _sunny_\n- see [forecast](https://w)\n\n\nLater: `clouds`"
    assert strip_markdown(text) == "Weather\n31°C and sunny\nsee forecast\nLater: clouds"


# --- HTTP client ----------------------------------------------------------------------


def _response(status=200, payload=INTERACTION):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def test_grounded_search_posts_the_interactions_request_with_the_bearer_key():
    with mock.patch.object(web_search.requests, "post", return_value=_response()) as post:
        res = grounded_search(
            "Who won the euro 2024?", url="https://relay/interactions",
            api_key="k", model="gemini-3.7-flash", timeout_s=7.0,
        )
    assert res.answer.startswith("Spain")
    assert res.elapsed_s >= 0.0
    (url,), kw = post.call_args
    assert url == "https://relay/interactions"
    assert kw["timeout"] == 7.0
    assert kw["headers"]["Authorization"] == "Bearer k"
    assert kw["json"] == {
        "model": "gemini-3.7-flash",
        "input": "Who won the euro 2024?",
        "tools": [{"type": "google_search"}],
    }


@pytest.mark.parametrize(
    "side_effect, match",
    [
        (requests.Timeout(), "timed out"),
        (requests.ConnectionError(), "request failed"),
    ],
)
def test_grounded_search_wraps_transport_failures(side_effect, match):
    with mock.patch.object(web_search.requests, "post", side_effect=side_effect):
        with pytest.raises(WebSearchError, match=match):
            grounded_search("q", url="https://relay", api_key="k", model="m", timeout_s=1.0)


def test_grounded_search_reports_a_non_200_and_a_missing_endpoint():
    with mock.patch.object(web_search.requests, "post", return_value=_response(status=401)):
        with pytest.raises(WebSearchError, match="HTTP 401"):
            grounded_search("q", url="https://relay", api_key="k", model="m", timeout_s=1.0)
    with pytest.raises(WebSearchError, match="not configured"):
        grounded_search("q", url="", api_key="k", model="m", timeout_s=1.0)


# --- orchestrator handler ---------------------------------------------------------------


def _orchestrator_with_agent():
    orch = object.__new__(RealtimeOrchestrator)
    orch._agent = mock.Mock()
    return orch


def _call(arguments='{"query": "weather in Hanoi today"}'):
    return FunctionCallOutput(name=WEB_SEARCH_TOOL_NAME, arguments=arguments, call_id="ws-1")


def _sent(orch):
    assert orch._agent.send.call_count == 1
    (payload,), _ = orch._agent.send.call_args
    assert len(payload) == 1
    return payload[0]


def test_handler_feeds_the_answer_back_and_lets_the_model_speak():
    orch = _orchestrator_with_agent()
    res = SearchResult(answer="About 31 degrees and sunny.", sources=["weather.com"], elapsed_s=3.9)
    with mock.patch.object(web_search, "grounded_search", return_value=res) as search:
        orch._handle_web_search_call(_call())
    assert search.call_args[0] == ("weather in Hanoi today",)
    sent = _sent(orch)
    assert sent.call_id == "ws-1"
    assert sent.trigger_response is True, "the follow-up reply IS the spoken answer"
    assert json.loads(sent.output) == {
        "result": "About 31 degrees and sunny.",
        "sources": ["weather.com"],
    }


def test_handler_reads_the_endpoint_knobs_from_config(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_PIPECAT_SEARCH_URL", "https://relay/x")
    monkeypatch.setattr(hal_config, "REALTIME_PIPECAT_SEARCH_API_KEY", "key-1")
    monkeypatch.setattr(hal_config, "REALTIME_PIPECAT_SEARCH_MODEL", "gemini-test")
    monkeypatch.setattr(hal_config, "REALTIME_PIPECAT_SEARCH_TIMEOUT_S", 4.5)
    orch = _orchestrator_with_agent()
    with mock.patch.object(web_search, "grounded_search", return_value=SearchResult(answer="ok")) as search:
        orch._handle_web_search_call(_call())
    assert search.call_args[1] == {
        "url": "https://relay/x", "api_key": "key-1", "model": "gemini-test", "timeout_s": 4.5,
    }


def test_handler_failure_is_still_acknowledged_with_a_way_out():
    """An unanswered call only times out into the bridge's generic error; a
    failed one must let the model say so or delegate — never guess."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(web_search, "grounded_search", side_effect=WebSearchError("search relay returned HTTP 503")):
        orch._handle_web_search_call(_call())
    sent = _sent(orch)
    assert sent.trigger_response is True
    body = json.loads(sent.output)
    assert body["error"] == "search relay returned HTTP 503"
    assert "delegate_to_main" in body["hint"]


def test_handler_refuses_an_empty_query_without_searching():
    orch = _orchestrator_with_agent()
    for arguments in ('{"query": "   "}', "{}", "not json", ""):
        orch._agent.send.reset_mock()
        with mock.patch.object(web_search, "grounded_search") as search:
            orch._handle_web_search_call(_call(arguments))
        assert not search.called, arguments
        assert json.loads(_sent(orch).output) == {"error": "query must not be empty"}


# --- stream_output: the turn continues through a search ---------------------------------


class _SearchingAgent:
    """A pipecat-shaped agent: the model calls web_search, then (once the
    result is in) speaks the answer."""

    def __init__(self) -> None:
        self.sent = []
        self.end_turn_calls = 0
        self.execution_completed = True
        self.execution_turn_id = "t-1"

    def receive(self, stop_on_done=True):
        del stop_on_done
        yield FunctionCallOutput(name=WEB_SEARCH_TOOL_NAME, arguments='{"query": "q"}', call_id="ws-1")
        assert self.sent, "the answer must be sent BEFORE the model can continue"
        yield TextOutput(text="About 31 degrees.")

    def send(self, inputs) -> None:
        self.sent.append(inputs)

    def end_turn(self) -> None:
        self.end_turn_calls += 1


def _orchestrator_for_stream(agent):
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._looked_this_turn = False
    orchestrator._skip_post_idle_recycle = False
    orchestrator._consecutive_silent = 3
    orchestrator._last_turn_monotonic = 0.0
    orchestrator._turns_since_recycle = 0
    orchestrator._idle_reset_pending = False
    return orchestrator


def test_stream_output_answers_the_search_and_keeps_the_turn(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_SESSION_MAX_TURNS", 0)
    agent = _SearchingAgent()
    orchestrator = _orchestrator_for_stream(agent)
    with mock.patch.object(web_search, "grounded_search", return_value=SearchResult(answer="31 °C")):
        outputs = list(orchestrator.stream_output())

    # The call itself is consumed; only the spoken follow-up reaches the caller.
    assert [type(o).__name__ for o in outputs] == ["TextOutput"]
    assert agent.end_turn_calls == 0, "unlike delegate/reject, a search does not leave the turn"
    assert json.loads(agent.sent[0][0].output)["result"] == "31 °C"
    assert orchestrator.execution_completed is True
    assert orchestrator._consecutive_silent == 0, "a turn that searched is not a silent turn"


# --- registration gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, flag, expected",
    [
        ("pipecat_v1", True, True),
        ("pipecat_v1", False, False),
        ("gemini", True, False),   # grounds on its own side
        ("gptlive", True, False),  # its Responses backend searches
        ("openai", True, False),
    ],
)
def test_web_search_is_registered_for_pipecat_only(monkeypatch, provider, flag, expected):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", provider)
    monkeypatch.setattr(hal_config, "REALTIME_PIPECAT_WEB_SEARCH", flag)
    assert orch_mod._web_search_available() is expected


def test_tool_schema_requires_a_query():
    assert WEB_SEARCH_TOOL["name"] == WEB_SEARCH_TOOL_NAME
    assert WEB_SEARCH_TOOL["parameters"]["required"] == ["query"]
    assert "delegate_to_main" in WEB_SEARCH_TOOL["description"]
