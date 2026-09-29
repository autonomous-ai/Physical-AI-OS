"""Long conversations must not lose turns between the window and summary.md (#449).

Device-observed 2026-09-28 on green-lamp: with a full summary the shared 8k
window held ~8 turns, the opening turn (the quiz/debate rules) fell out at
turn ~10, and the summarizer only ran at turn ~16 — so those turns reached no
session at all and the lamp stopped following the activity.
"""

import json
import re
import threading
from types import SimpleNamespace

import hal.config as app_config
from hal.realtime.context_manager import base as base_mod
from hal.realtime.context_manager.openclaw import OpenClawContextManager


class _FakeSummarizer:
    """Summary = the turn ids it has seen, so coverage is checkable."""

    def __init__(self, result: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.called = threading.Event()
        self._result = result

    def summarize(self, entries: list[str]) -> str:
        self.calls.append(list(entries))
        self.called.set()
        if self._result is not None:
            return self._result
        ids = sorted(set(re.findall(r"T\d{3}", "\n".join(entries))))
        return "covered " + " ".join(ids)


def _manager(tmp_path, summarizer=None, max_chars=1000) -> OpenClawContextManager:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return OpenClawContextManager(
        workspace_dir=str(workspace),
        realtime_memory_path=str(tmp_path / "realtime" / "memory.jsonl"),
        realtime_memory_max_chars=max_chars,
        summarizer=summarizer,
    )


def _write_turns(manager, ids: list[str], size: int = 150) -> None:
    path = manager._realtime_memory_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for tid in ids:
            f.write(json.dumps({"ts": "t", "user": f"{tid} " + "u" * size, "agent": "a"}) + "\n")


def _verbatim_ids(manager) -> set[str]:
    loaded = manager.load_realtime_memory()
    return {tid for e in loaded if not e.startswith("[Previous summary]")
            for tid in re.findall(r"T\d{3}", e)}


def test_verbatim_turns_have_their_own_budget(tmp_path):
    manager = _manager(tmp_path, max_chars=1000)
    manager._summary_path.parent.mkdir(parents=True, exist_ok=True)
    manager._summary_path.write_text("s" * 900, encoding="utf-8")
    _write_turns(manager, ["T001", "T002", "T003"])  # 176 chars each formatted

    # Before: 900 summary chars left room for no turn at all.
    assert _verbatim_ids(manager) == {"T001", "T002", "T003"}


def test_summarize_triggers_before_the_window_overflows(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARIZE_AT_FRACTION", 0.75)
    fake = _FakeSummarizer()
    manager = _manager(tmp_path, summarizer=fake, max_chars=1000)
    # add_turn stamps a real ISO timestamp: each formatted entry is 177 chars.
    for i in range(1, 5):  # 4 × 177 = 708 chars: under 750
        manager.add_turn(f"T{i:03d} " + "u" * 120, "a")
    assert not fake.called.wait(0.3)
    manager.add_turn("T005 " + "u" * 120, "a")  # 885 chars: over 750, under 1000
    assert fake.called.wait(2.0)


def test_summarize_keeps_the_newest_turns_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 4)
    fake = _FakeSummarizer()
    manager = _manager(tmp_path, summarizer=fake, max_chars=2000)  # tail cap 1000: count governs
    _write_turns(manager, [f"T{i:03d}" for i in range(1, 7)])

    manager.summarize_realtime_memory()

    summarized = "\n".join(fake.calls[0])
    assert "T001" in summarized and "T002" in summarized
    assert "T003" not in summarized
    assert _verbatim_ids(manager) == {"T003", "T004", "T005", "T006"}


def test_summarize_is_a_noop_when_only_recent_turns_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 4)
    fake = _FakeSummarizer()
    manager = _manager(tmp_path, summarizer=fake, max_chars=2000)  # tail cap 1000: count governs
    _write_turns(manager, ["T001", "T002", "T003"])

    manager.summarize_realtime_memory()

    assert fake.calls == []
    assert not manager._summary_path.exists()
    assert _verbatim_ids(manager) == {"T001", "T002", "T003"}


def test_turn_saved_during_summarize_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 1)
    manager = _manager(tmp_path)

    class _SavesMidway(_FakeSummarizer):
        def summarize(self, entries):
            _write_turns(manager, ["T099"])  # a voice turn lands mid-summarize
            return super().summarize(entries)

    manager._summarizer = _SavesMidway()
    _write_turns(manager, ["T001", "T002", "T003"])

    manager.summarize_realtime_memory()

    assert _verbatim_ids(manager) == {"T003", "T099"}


def test_no_turn_is_ever_in_neither_place(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARIZE_AT_FRACTION", 0.75)
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 2)

    class _InlineThread:
        def __init__(self, target, **_kw):
            self._target = target

        def start(self):
            self._target()

    # Run the background summarize inline, for this module only (patching
    # threading.Thread itself would leak into every other thread).
    monkeypatch.setattr(
        base_mod, "threading", SimpleNamespace(Thread=_InlineThread, Lock=threading.Lock)
    )
    manager = _manager(tmp_path, summarizer=_FakeSummarizer(), max_chars=1000)

    for i in range(1, 41):
        manager.add_turn(f"T{i:03d} " + "u" * 150, "a")
        summary = manager._summary_path.read_text(encoding="utf-8") if manager._summary_path.exists() else ""
        covered = set(re.findall(r"T\d{3}", summary)) | _verbatim_ids(manager)
        missing = {f"T{j:03d}" for j in range(1, i + 1)} - covered
        assert not missing, f"after turn {i}: {sorted(missing)} in neither place"


def test_kept_tail_is_bounded_by_chars(tmp_path, monkeypatch):
    # Four long replies must not all stay verbatim: the tail alone would fill
    # the window and trigger a summarize on every following turn.
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 4)
    manager = _manager(tmp_path, summarizer=_FakeSummarizer(), max_chars=1000)
    _write_turns(manager, [f"T{i:03d}" for i in range(1, 7)], size=300)  # 321 chars each

    manager.summarize_realtime_memory()

    # Half the verbatim budget (500 chars) holds one 321-char turn.
    assert _verbatim_ids(manager) == {"T006"}


def test_no_turn_is_ever_in_neither_place_with_long_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "REALTIME_SUMMARIZE_AT_FRACTION", 0.75)
    monkeypatch.setattr(app_config, "REALTIME_SUMMARY_KEEP_RECENT_TURNS", 4)

    class _InlineThread:
        def __init__(self, target, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(
        base_mod, "threading", SimpleNamespace(Thread=_InlineThread, Lock=threading.Lock)
    )
    fake = _FakeSummarizer()
    manager = _manager(tmp_path, summarizer=fake, max_chars=1000)

    for i in range(1, 31):
        manager.add_turn(f"T{i:03d} " + "u" * 300, "a")  # ~357 chars formatted
        summary = manager._summary_path.read_text(encoding="utf-8") if manager._summary_path.exists() else ""
        covered = set(re.findall(r"T\d{3}", summary)) | _verbatim_ids(manager)
        missing = {f"T{j:03d}" for j in range(1, i + 1)} - covered
        assert not missing, f"after turn {i}: {sorted(missing)} in neither place"
    # A summarize per turn would mean the kept tail alone re-triggers it.
    assert len(fake.calls) < 20
