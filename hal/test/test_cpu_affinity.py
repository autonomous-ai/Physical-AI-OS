"""CPU pinning: fast/slow split from sysfs capacities, env gates, and what gets pinned where."""

import threading

import pytest

from hal import cpu_affinity


def cpuinfo(parts, implementer="0x41"):
    """A /proc/cpuinfo body in the aarch64 kernel's format, one block per core."""
    return "\n\n".join(
        f"processor\t: {cpu}\nBogoMIPS\t: 48.00\nCPU implementer\t: {implementer}\n"
        f"CPU architecture: 8\nCPU part\t: {part}\nCPU revision\t: 0"
        for cpu, part in enumerate(parts)
    ) + "\n"


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Fake /proc/cpuinfo + affinity syscalls; returns (write cores, recorded setaffinity calls)."""
    calls = []
    path = tmp_path / "cpuinfo"
    monkeypatch.setattr(cpu_affinity, "CPUINFO_PATH", str(path))
    monkeypatch.setattr(cpu_affinity.read_core_parts, "__defaults__", (str(path),))
    monkeypatch.setattr(cpu_affinity.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
    monkeypatch.setattr(cpu_affinity.os, "sched_setaffinity", lambda pid, cpus: calls.append((pid, set(cpus))), raising=False)
    monkeypatch.setenv("HAL_CPU_PINNING", "1")
    monkeypatch.delenv("HAL_FAST_CPUS", raising=False)
    cpu_affinity.core_sets.cache_clear()

    def write(parts, implementer="0x41"):
        path.write_text(cpuinfo(parts, implementer))
        cpu_affinity.core_sets.cache_clear()

    yield write, calls
    cpu_affinity.core_sets.cache_clear()


# lamp-ee17 (Allwinner sun60iw2): six Cortex-A55 + two Cortex-A76, as its /proc/cpuinfo reports.
LAMP = ["0xd05"] * 6 + ["0xd0b"] * 2


def test_the_lamps_a76_cores_are_fast_and_its_a55_cores_slow(board):
    write, _ = board
    write(LAMP)
    assert cpu_affinity.core_sets() == (frozenset({6, 7}), frozenset(range(6)))


def test_an_rk3588_splits_four_a55_from_four_a76(board):
    write, _ = board
    write(["0xd05"] * 4 + ["0xd0b"] * 4)
    assert cpu_affinity.core_sets() == (frozenset({4, 5, 6, 7}), frozenset(range(4)))


@pytest.mark.parametrize("parts", [["0xd0b"] * 8, ["0xd08"] * 8], ids=["pi5-a76", "pi4-a72"])
def test_a_board_with_one_core_tier_is_never_pinned(board, parts):
    write, calls = board
    write(parts)
    assert cpu_affinity.core_sets() is None
    assert cpu_affinity.pin_current_thread(cpu_affinity.FAST) is False
    assert calls == []


def test_a_core_model_missing_from_the_table_turns_pinning_off(board):
    write, _ = board
    write(["0xd05"] * 6 + ["0xfff"] * 2)
    assert cpu_affinity.core_sets() is None


def test_a_non_arm_implementer_is_not_looked_up(board):
    write, _ = board
    write(LAMP, implementer="0x51")  # Qualcomm reuses part numbers for its own cores
    assert cpu_affinity.core_sets() is None


def test_an_unreadable_cpuinfo_turns_pinning_off(board):
    assert cpu_affinity.core_sets() is None  # fixture never wrote the file


def test_pinning_is_off_unless_the_env_flag_turns_it_on(board, monkeypatch):
    write, calls = board
    monkeypatch.setenv("HAL_CPU_PINNING", "0")
    write(LAMP)
    assert cpu_affinity.pin_current_thread(cpu_affinity.FAST) is False
    assert calls == []


def test_the_env_override_picks_the_fast_set(board, monkeypatch):
    write, _ = board
    monkeypatch.setenv("HAL_FAST_CPUS", "4-5,7")
    write(["0xfff"] * 8)  # the table is not consulted once overridden
    assert cpu_affinity.core_sets() == (frozenset({4, 5, 7}), frozenset({0, 1, 2, 3, 6}))


@pytest.mark.parametrize("override", ["abc", "0-7"])
def test_an_override_that_leaves_no_split_turns_pinning_off(board, monkeypatch, override):
    write, _ = board
    monkeypatch.setenv("HAL_FAST_CPUS", override)
    write(LAMP)
    assert cpu_affinity.core_sets() is None


def test_fast_and_slow_roles_pin_the_calling_thread_to_their_cores(board):
    write, calls = board
    write(LAMP)
    assert cpu_affinity.pin_current_thread(cpu_affinity.FAST)
    assert cpu_affinity.pin_current_thread(cpu_affinity.SLOW)
    assert calls == [(0, {6, 7}), (0, set(range(6)))]


def test_a_child_process_is_pinned_by_pid(board):
    write, calls = board
    write(LAMP)
    assert cpu_affinity.pin_pid(4242, cpu_affinity.FAST)
    assert calls == [(4242, {6, 7})]


def test_a_failed_pin_is_reported_not_raised(board, monkeypatch):
    write, _ = board
    write(LAMP)

    def refuse(pid, cpus):
        raise ProcessLookupError("child already exited")

    monkeypatch.setattr(cpu_affinity.os, "sched_setaffinity", refuse)
    assert cpu_affinity.pin_pid(4242, cpu_affinity.FAST) is False


def test_on_cores_pins_inside_the_new_thread_before_the_target_runs(board):
    write, calls = board
    write(LAMP)
    seen = []

    def target(x):
        seen.append((x, list(calls)))

    t = threading.Thread(target=cpu_affinity.on_cores(cpu_affinity.FAST, target), args=("frame",))
    t.start()
    t.join()
    assert seen == [("frame", [(0, {6, 7})])]


def test_the_sensing_pool_threads_start_on_the_slow_cores():
    from hal.drivers.sensing.perceptions.processors.base import Perception

    pool = Perception._pool
    assert pool._initializer is cpu_affinity.pin_current_thread
    assert pool._initargs == (cpu_affinity.SLOW,)
