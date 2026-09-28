"""A failed summarization must say what the server actually sent."""

import logging
from unittest import mock

from hal.realtime.summarizer import RealtimeSummarizer


def test_undecodable_body_is_reported_as_bytes(caplog):
    exc = UnicodeDecodeError("utf-8", b"\x1f\x8b\x08\x00\xc4\x01", 4, 5, "invalid continuation byte")
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)
    logged = caplog.text
    assert "undecodable response body" in logged
    assert "1f 8b 08 00 c4" in logged, logged


def test_http_error_reports_status_and_headers(caplog):
    response = mock.Mock()
    response.status_code = 502
    response.headers = {"content-type": "text/html", "content-encoding": "br"}
    response.content = b"<html>Bad Gateway</html>"
    response.request.url = "https://example.invalid/v1/messages"
    exc = RuntimeError("boom")
    exc.response = response
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)
    logged = caplog.text
    assert "HTTP 502" in logged
    assert "text/html" in logged and "'br'" in logged
    assert "Bad Gateway" in logged


def test_broken_response_object_does_not_raise(caplog):
    exc = RuntimeError("boom")
    exc.response = object()
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)


def test_plain_exception_logs_nothing_extra(caplog):
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(ValueError("no response attached"))
    assert caplog.text == ""


class _FakeStream:
    def __init__(self, parts):
        self.text_stream = iter(parts)

    def get_final_message(self):
        return mock.Mock(stop_reason="max_tokens", usage=mock.Mock(output_tokens=400))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ready_summarizer(stream_parts):
    s = RealtimeSummarizer.__new__(RealtimeSummarizer)
    s._system_prompt = "sys"
    s._model = "m"
    s._base_url = "https://example.invalid"
    s._retries, s._retry_backoff_s = 0, 0.0
    client = mock.Mock()
    client.messages.stream.return_value = _FakeStream(stream_parts)
    s._get_client = lambda: client
    return s, client


def test_summarize_streams_and_never_calls_the_non_streaming_endpoint():
    s, client = _ready_summarizer(["Long ", "asked ", "about lamps."])
    assert s.summarize(["user: hi", "lamp: hello"]) == "Long asked about lamps."
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()


def test_empty_entries_short_circuit_without_a_request():
    s, client = _ready_summarizer([])
    assert s.summarize(["", "   "]) == ""
    client.messages.stream.assert_not_called()


def test_a_stream_failure_returns_empty_instead_of_raising(caplog):
    s = RealtimeSummarizer.__new__(RealtimeSummarizer)
    s._system_prompt, s._model, s._base_url = "sys", "m", "u"
    s._retries, s._retry_backoff_s = 0, 0.0
    client = mock.Mock()
    client.messages.stream.side_effect = RuntimeError("gateway down")
    s._get_client = lambda: client
    assert s.summarize(["user: hi"]) == ""
    assert "Summarization failed" in caplog.text


def _retrying_summarizer(side_effects, retries=2):
    s = RealtimeSummarizer.__new__(RealtimeSummarizer)
    s._system_prompt, s._model, s._base_url = "sys", "m", "u"
    s._retries, s._retry_backoff_s = retries, 0.0
    client = mock.Mock()
    client.messages.stream.side_effect = side_effects
    s._get_client = lambda: client
    return s, client


def test_a_dropped_call_is_retried():
    s, client = _retrying_summarizer(
        [RuntimeError("gateway dropped it"), _FakeStream(["recovered"])]
    )
    assert s.summarize(["user: hi"]) == "recovered"
    assert client.messages.stream.call_count == 2


def test_it_gives_up_after_the_configured_number_of_retries():
    s, client = _retrying_summarizer([RuntimeError("down")] * 5, retries=2)
    assert s.summarize(["user: hi"]) == ""
    assert client.messages.stream.call_count == 3


def test_retries_can_be_switched_off():
    s, client = _retrying_summarizer([RuntimeError("down")] * 5, retries=0)
    assert s.summarize(["user: hi"]) == ""
    assert client.messages.stream.call_count == 1


def test_a_first_attempt_that_works_is_not_repeated():
    s, client = _retrying_summarizer([_FakeStream(["fine"])])
    assert s.summarize(["user: hi"]) == "fine"
    assert client.messages.stream.call_count == 1


def test_empty_success_is_failure_and_returns_promptly_for_caller_fallback(caplog):
    s, client = _retrying_summarizer([_FakeStream([])], retries=2)
    s._max_tokens = 400
    assert s.summarize(["A completed task"]) == ""
    assert client.messages.stream.call_count == 1
    assert "Empty summarizer response" in caplog.text
    assert "stop_reason=max_tokens" in caplog.text
    assert "output_tokens=400" in caplog.text
    assert "Summarized" not in caplog.text


def test_notification_disables_thinking_without_changing_memory_defaults():
    s, client = _ready_summarizer(["Done."])
    s._disable_thinking = True
    assert s.summarize(["result"]) == "Done."
    assert client.messages.stream.call_args.kwargs["thinking"] == {"type": "disabled"}
    memory, memory_client = _ready_summarizer(["Memory."])
    assert memory.summarize(["history"]) == "Memory."
    assert "thinking" not in memory_client.messages.stream.call_args.kwargs
