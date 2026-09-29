"""Compose independent sensor workers into one environmental snapshot."""

import math
import logging
from pathlib import Path

from hal.drivers.environment.registry import COMPONENTS, MEASUREMENTS
from hal.drivers.environment.service import EnvironmentService

logger = logging.getLogger(__name__)


def create_environment_group(device_dir, board_id, simulation=False):
    # Old installed profiles may retain this file after OTA. It no longer
    # selects hardware: each sensor's enabled flag is the sole component gate.
    if (Path(device_dir) / "environment.json").exists():
        logger.info("[environment] ignoring legacy environment.json; using per-sensor enabled flags")
    workers = {}
    for name, component in COMPONENTS.items():
        cfg = None if simulation else component.loader(device_dir, board_id)
        workers[name] = EnvironmentService(
            enabled=cfg is not None,
            bus=cfg.bus if cfg else None,
            timing=cfg.timing if cfg else component.timing(),
            driver_factory=component.factory(cfg),
            name=name,
        )
    return EnvironmentGroup(workers)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class EnvironmentGroup:
    def __init__(self, components):
        self.components = dict(components)
        self.sources = {}
        for name, worker in self.components.items():
            if not worker.enabled:
                continue
            for field in COMPONENTS[name].measurements:
                if field in self.sources:
                    raise ValueError(
                        f"Environment metric {field} is provided by enabled components "
                        f"{self.sources[field]} and {name}; disable one in its sensor JSON"
                    )
                self.sources[field] = name

    def start(self):
        for worker in self.components.values():
            worker.start()

    def stop(self):
        for worker in self.components.values():
            worker.stop()

    def snapshot(self):
        components = {name: worker.snapshot() for name, worker in self.components.items()}
        sample = dict.fromkeys(MEASUREMENTS)
        sample["timestamp"] = None
        sources, timestamps = dict(self.sources), {}
        ages, errors, active = [], [], []
        unavailable = False
        for name, component in components.items():
            fields = COMPONENTS[name].measurements
            if not component["enabled"]:
                continue
            active.append(component)
            values = component.get("sample") or {}
            stamp, age = values.get("timestamp"), component.get("age_s")
            status = values.get("device_status")
            valid = (
                component["state"] == "ready" and not component["stale"]
                and _finite(stamp) and stamp > 0
                and _finite(age) and age >= 0
                and (status is None or status == 0)
            )
            if component.get("last_error"):
                errors.append(f"{name}: {component['last_error']}")
            elif status is not None and status != 0:
                errors.append(f"{name}: sensor status reports a fault ({status})")
            added = False
            if valid:
                for field in fields:
                    value = values.get(field)
                    if _finite(value):
                        sample[field] = value
                        timestamps[field] = stamp
                        added = True
                if added:
                    ages.append(age)
            if not added:
                unavailable = True

        ready = bool(timestamps)
        if ready:
            sample["timestamp"] = max(timestamps.values())
            state = "ready"
        elif not active:
            state = "disabled"
        elif errors or any(c["state"] == "error" for c in active):
            state = "error"
        elif all(c["state"] == "stopped" for c in active):
            state = "stopped"
        else:
            state = "starting"
        result = {
            "state": state, "enabled": bool(active), "stale": not ready,
            "partial": ready and unavailable,
            "last_error": "; ".join(errors) or None,
            "sample": sample,
            "age_s": min(ages) if ages else None,
            "components": components,
            "sources": sources,
            "metric_timestamps": timestamps,
        }
        if len(components) == 1:
            only = next(iter(components.values()))
            result.update(bus=only["bus"], timing=only["timing"])
            if ready and "device_status" in only["sample"]:
                sample["device_status"] = only["sample"]["device_status"]
        return result
