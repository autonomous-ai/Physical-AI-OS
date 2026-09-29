"""The realtime memory summarizer must produce text, and say so when it can't (#449).

Device-observed 2026-09-28: `stop_reason=max_tokens output_tokens=4096` with no
text — provider-default thinking spent the whole budget — and nothing but a
WARNING said memory had stopped being compressed.
"""

import json
import logging

from hal.realtime import orchestrator as orch_mod
from hal.realtime.context_manager.openclaw import OpenClawContextManager


def test_memory_summarizer_disables_thinking(monkeypatch):
    seen: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(orch_mod, "RealtimeSummarizer", _Recorder)
    orch_mod._make_memory_summarizer()
    assert seen.get("disable_thinking") is True


class _Empty:
    def summarize(self, entries):
        return ""


def test_repeated_empty_summaries_escalate_and_keep_memory(tmp_path, caplog, monkeypatch):
    import hal.config as app_config
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 0)
    (tmp_path / "workspace").mkdir()
    manager = OpenClawContextManager(
        workspace_dir=str(tmp_path / "workspace"),
        realtime_memory_path=str(tmp_path / "realtime" / "memory.jsonl"),
        summarizer=_Empty(),
    )
    manager._realtime_memory_path.parent.mkdir(parents=True)
    line = json.dumps({"ts": "t", "user": "u", "agent": "a"})
    manager._realtime_memory_path.write_text(line + "\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        manager.summarize_realtime_memory()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        manager.summarize_realtime_memory()

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "2 in a row" in errors[-1].getMessage()
    assert manager._realtime_memory_path.read_text(encoding="utf-8").strip() == line
    assert not manager._summary_path.exists()
