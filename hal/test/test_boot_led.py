import subprocess
from unittest.mock import patch

import pytest

from hal.server_support.boot_led import stop_boot_indicator


def test_without_device_service_no_systemd_call():
    with patch('hal.server_support.boot_led.Path.exists', return_value=False), \
            patch('hal.server_support.boot_led.subprocess.run') as run:
        stop_boot_indicator()
        run.assert_not_called()


@pytest.mark.parametrize('exists,unit', [
    ([False, True], 'led-boot.service'),
    ([True, False], 'lamp-led-boot.service'),
])
def test_stop_waits_and_requires_success(exists, unit):
    with patch('hal.server_support.boot_led.Path.exists', side_effect=exists), \
            patch('hal.server_support.boot_led.subprocess.run') as run:
        stop_boot_indicator()
        run.assert_called_once_with(
            ['systemctl', 'stop', unit], check=True, timeout=5)


@pytest.mark.parametrize('error', [
    subprocess.CalledProcessError(1, 'systemctl'),
    subprocess.TimeoutExpired('systemctl', 5),
])
def test_failed_handoff_propagates_before_spi_can_open(error):
    with patch('hal.server_support.boot_led.Path.exists', return_value=True), \
            patch('hal.server_support.boot_led.subprocess.run', side_effect=error):
        with pytest.raises(type(error)):
            stop_boot_indicator()
