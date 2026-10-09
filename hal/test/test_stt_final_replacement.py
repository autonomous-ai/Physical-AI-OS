"""Provider finals replace provisional words in both real capture pipelines."""

import pytest

from hal.test.test_device_voice_pipeline import (
    Session,
    capture_turn,
    setup_pipeline,  # noqa: F401
)
from hal.test.test_turn_endpoint_capture import capture


TRANSCRIPTS = [
    pytest.param([("Forty six plus six.", False), ("46 plus 6.", True)],
                 "46 plus 6.", id="numeric-normalization"),
    pytest.param([("One fifty six plus six.", False), ("1566.", True)],
                 "1566.", id="numeric-correction"),
    pytest.param([("Please arrange a dinner reservation.", False), ("Cancel it.", True)],
                 "Cancel it.", id="short-correction"),
    pytest.param([("Forty six plus six.", False), ("46 plus 6.", True),
                  ("And twenty five more.", False), ("And 25 more.", True)],
                 "46 plus 6. And 25 more.", id="multiple-final-segments"),
    pytest.param([("Please check the weather tomorrow.", False)],
                 "Please check the weather tomorrow.", id="no-final-fallback"),
    pytest.param([("Please plan a trip.", True), ("And reserve a hotel.", False)],
                 "Please plan a trip. And reserve a hotel.", id="unfinished-trailing-segment"),
]


@pytest.mark.parametrize("transcripts,expected", TRANSCRIPTS)
def test_automatic_capture_dispatches_finals_without_resurrecting_partial(
        monkeypatch, transcripts, expected):
    frames = [(1 + index * 0.1, True, transcript)
              for index, transcript in enumerate(transcripts)]
    frames.append((4 + len(transcripts) * 0.1, False, None))
    with capture(monkeypatch, frames) as result:
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == expected


@pytest.mark.parametrize("transcripts,expected", TRANSCRIPTS)
def test_device_capture_dispatches_finals_without_resurrecting_partial(
        setup_pipeline, transcripts, expected):
    class TranscriptSession(Session):
        def close(self):
            self.closed += 1
            for text, final in transcripts:
                self.callback(text, final)

    setup_pipeline.sessions.append(TranscriptSession(""))
    ticket, _, accepted = capture_turn(setup_pipeline, [b"first", b"last"])
    assert accepted
    assert ticket.done.wait(2)
    assert len(setup_pipeline.dispatches) == 1
    assert setup_pipeline.dispatches[0][0] == expected
