"""Regression coverage for SEN63C on Sunxi's default 400 kHz bus."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hal.drivers.environment.i2c import limit_sunxi_bus_clock
from hal.drivers.environment.sen63c import SEN63C
from hal.drivers.environment.group import create_environment_group


def adapter(root, frequency=400_000, name="SUNXI TWI(0x0000000002510000)"):
    path = root / "i2c-2"
    (path / "device").mkdir(parents=True)
    (path / "name").write_text(name)
    (path / "device/info").write_text(f"twi->bus_num = 2\ntwi->freqency = {frequency}\n")
    return path


def test_lowers_selected_bus_and_checks_runtime_readback(tmp_path):
    path = adapter(tmp_path)
    original = Path.write_text

    def kernel_write(target, value):
        assert target == path / "device/freq"
        assert value == "100000\n"
        original(path / "device/info", "twi->freqency = 100000\n")
        return len(value)

    with patch.object(Path, "write_text", kernel_write):
        limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)
    assert not (tmp_path / "i2c-0").exists()


@pytest.mark.parametrize("frequency", [50_000, 100_000])
def test_never_raises_an_already_compatible_clock(tmp_path, frequency):
    adapter(tmp_path, frequency)
    with patch.object(Path, "write_text") as write:
        limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)
    write.assert_not_called()


def test_other_controller_or_missing_sysfs_is_untouched(tmp_path):
    limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)
    adapter(tmp_path, name="bcm2835 I2C adapter")
    with patch.object(Path, "write_text") as write:
        limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)
    write.assert_not_called()


@pytest.mark.parametrize("info", ["", "twi->freqency = 0\n", "twi->freqency = invalid\n"])
def test_unknown_sunxi_frequency_fails_before_writing(tmp_path, info):
    path = adapter(tmp_path)
    (path / "device/info").write_text(info)
    with pytest.raises(OSError, match="cannot enforce maximum 100000 Hz"):
        limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)
    assert not (path / "device/freq").exists()


def test_rejects_unsuccessful_clock_change(tmp_path):
    adapter(tmp_path)
    with pytest.raises(OSError, match="clock remains 400000 Hz"):
        limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)


def test_permission_failure_is_actionable(tmp_path):
    adapter(tmp_path)
    with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
        with pytest.raises(OSError, match="I2C bus 2: cannot enforce maximum 100000 Hz"):
            limit_sunxi_bus_clock(2, 100_000, sysfs_root=tmp_path)


def test_driver_prepares_clock_before_opening_sensor():
    with patch("hal.drivers.environment.sen63c.limit_sunxi_bus_clock", side_effect=OSError("clock failed")) as clock:
        with patch("os.open") as opened:
            with pytest.raises(OSError, match="clock failed"):
                SEN63C(2)
    clock.assert_called_once_with(2, 100_000)
    opened.assert_not_called()


@pytest.mark.parametrize("simulation", [False, True])
def test_disabled_or_simulated_sensor_never_changes_clock(tmp_path, simulation):
    (tmp_path / "sen63c.json").write_text(
        '{"boards":{"test":{"enabled":' + str(simulation).lower() + ',"bus":2}}}'
    )
    with patch("hal.drivers.environment.sen63c.limit_sunxi_bus_clock") as clock:
        group = create_environment_group(str(tmp_path), "test", simulation=simulation)
        group.start()
        group.stop()
    clock.assert_not_called()
