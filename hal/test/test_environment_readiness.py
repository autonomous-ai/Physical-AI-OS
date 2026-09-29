"""Acquisition continuity for environment context across OS restarts."""

from unittest.mock import Mock, patch

import pytest

from hal.drivers.environment.service import EnvironmentService


def snapshot_at(service, now):
    with patch("hal.drivers.environment.service.time.monotonic", return_value=now):
        return service.snapshot()


def test_continuity_advances_only_with_samples():
    service = EnvironmentService(True, 1)
    assert service.snapshot()["continuous_data_s"] is None
    service._store_sample({"co2_ppm": 600}, 100)
    assert snapshot_at(service, 100)["continuous_data_s"] == 0
    service._store_sample({"co2_ppm": 601}, 104)
    assert snapshot_at(service, 104)["continuous_data_s"] == 4
    assert snapshot_at(service, 108)["continuous_data_s"] == 4
    assert snapshot_at(service, 110)["continuous_data_s"] is None
    service._store_sample({"co2_ppm": 602}, 110)
    assert snapshot_at(service, 110)["continuous_data_s"] == 0


@pytest.mark.parametrize("state", ["starting", "error", "stopped", "disabled"])
def test_state_transition_resets_continuity(state):
    service = EnvironmentService(True, 1)
    service._store_sample({"co2_ppm": 600}, 100)
    service._store_sample({"co2_ppm": 601}, 104)
    service._set_state(state)
    assert snapshot_at(service, 104)["continuous_data_s"] is None
    service._store_sample({"co2_ppm": 602}, 105)
    assert snapshot_at(service, 105)["continuous_data_s"] == 0


def test_disabled_and_legacy_cache_have_no_readiness():
    service = EnvironmentService(False, 1)
    service._store_sample({"co2_ppm": 600}, 100)
    assert snapshot_at(service, 100)["continuous_data_s"] is None
    service.enabled = True
    service._first_sample_time = None
    assert snapshot_at(service, 100)["continuous_data_s"] is None


def test_worker_retry_and_not_ready_reads_do_not_preserve_continuity():
    clock = [100.0]
    service = EnvironmentService(True, 1)
    driver = Mock()
    service._factory = lambda _: driver
    outcomes = iter([
        (100, {"co2_ppm": 600}),
        (104, {"co2_ppm": 601}),
        (105, None),
        (106, OSError("read failed")),
        (107, {"co2_ppm": 602}),
    ])
    observed = []

    def read():
        clock[0], result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    def wait(_):
        observed.append(service.snapshot())
        if driver.read.call_count == 5:
            service._stop.is_set.return_value = True
            return True
        return False

    driver.read.side_effect = read
    service._stop = Mock()
    service._stop.is_set.return_value = False
    service._stop.wait.side_effect = wait
    with patch("hal.drivers.environment.service.time.monotonic", side_effect=lambda: clock[0]):
        service._run()
    assert [s["continuous_data_s"] for s in observed] == [None, 0, 4, 4, None, None, 0, 0]
    assert driver.start.call_count == 2
    assert driver.close.call_count == 2
    assert service.snapshot()["continuous_data_s"] is None
