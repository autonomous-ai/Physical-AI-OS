"""Bind hardware components to the shared environmental measurement contract."""

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from hal.board.sen55 import load_sen55_config, SEN55Timing
from hal.board.scd41 import load_scd41_config, SCD41Timing
from hal.board.sen63c import load_sen63c_config, SEN63CTiming
from hal.drivers.environment.sen55 import SEN55, FIELDS as SEN55_FIELDS
from hal.drivers.environment.scd41 import SCD41
from hal.drivers.environment.sen63c import SEN63C, FIELDS as SEN63C_FIELDS


# Public software keys are independent of which hardware is installed.
MEASUREMENTS = (
    "pm1_0_ug_m3", "pm2_5_ug_m3", "pm4_0_ug_m3", "pm10_ug_m3",
    "humidity_pct", "temperature_c", "voc_index", "nox_index", "co2_ppm",
)


@dataclass(frozen=True)
class Component:
    loader: Callable[[str, str], Any]
    driver: Callable[..., Any]
    timing: Callable[[], SEN55Timing]
    measurements: tuple[str, ...]
    driver_options: tuple[str, ...] = ()

    def factory(self, config):
        options = {key: getattr(config, key) for key in self.driver_options} if config else {}
        return partial(self.driver, **options) if options else self.driver


COMPONENTS = {
    "sen55": Component(load_sen55_config, SEN55, SEN55Timing, SEN55_FIELDS),
    "scd41": Component(load_scd41_config, SCD41, SCD41Timing, ("co2_ppm",), ("automatic_self_calibration",)),
    "sen63c": Component(load_sen63c_config, SEN63C, SEN63CTiming, SEN63C_FIELDS, ("automatic_self_calibration",)),
}
