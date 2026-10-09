"""Read-only MPR121 signal capture; run from repo root with python -m.

No reset, register configuration, gesture dispatch, or automatic calibration.
The Linux adapter serializes repeated-start reads with HAL's polling transfers.
"""

import argparse
import json
import math
from pathlib import Path
import time

from hal.drivers.mpr121 import I2CBus


def read_exact(bus, address, register, size):
    data = bus.read_regs(address, register, size)
    if len(data) != size:
        raise OSError(f"Incomplete MPR121 read at 0x{register:02x}")
    return data


def read_sample(bus, address):
    # One transfer minimizes skew, but chip updates are not an atomic snapshot.
    data = read_exact(bus, address, 0, 42)
    status = int.from_bytes(data[:2], "little")
    oor = int.from_bytes(data[2:4], "little")
    filtered = [int.from_bytes(data[4 + 2*i:6 + 2*i], "little") & 0x3ff
                for i in range(12)]
    baseline = [value << 2 for value in data[30:42]]
    return {
        "mask": status & 0xfff,
        "overcurrent": bool(status & 0x8000),
        "out_of_range_mask": oor & 0xfff,
        "autoconfig_failed": bool(oor & 0x8000),
        "autoreconfig_failed": bool(oor & 0x4000),
        "filtered": filtered,
        "baseline": baseline,
        "delta": [base - value for base, value in zip(baseline, filtered)],
    }


def read_settings(bus, address):
    thresholds = read_exact(bus, address, 0x41, 24)
    config = read_exact(bus, address, 0x5b, 4)
    auto = read_exact(bus, address, 0x7b, 5)
    charge = read_exact(bus, address, 0x5f, 19)
    return {
        "touch_thresholds": list(thresholds[::2]),
        "release_thresholds": list(thresholds[1::2]),
        "debounce": config[0], "config1": config[1],
        "config2": config[2], "electrode_config": config[3],
        "autoconfig_registers": list(auto), "charge_registers": list(charge),
    }


def summarize(samples):
    if not samples:
        return {"samples": 0}
    electrodes = []
    for electrode in range(12):
        values = sorted(row["delta"][electrode] for row in samples)
        electrodes.append({
            "electrode": electrode,
            "delta_min": values[0],
            "delta_p50": values[math.ceil(len(values) * .5) - 1],
            "delta_p99": values[math.ceil(len(values) * .99) - 1],
            "delta_max": values[-1],
            "active_samples": sum(bool(row["mask"] & (1 << electrode)) for row in samples),
        })
    return {
        "samples": len(samples), "electrodes": electrodes,
        "mask_transitions": sum(a["mask"] != b["mask"] for a, b in zip(samples, samples[1:])),
        "fault_samples": sum(any(row[key] for key in (
            "overcurrent", "out_of_range_mask", "autoconfig_failed", "autoreconfig_failed",
        )) for row in samples),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, default=0)
    parser.add_argument("--address", type=lambda s: int(s, 0), default=0x5a)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--interval-ms", type=int, default=10)
    parser.add_argument("--label", default="unlabelled", help="Operator label, not an automatic touch classification")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.bus < 0 or not 0x5a <= args.address <= 0x5d
            or not 1 <= args.seconds <= 300 or not 1 <= args.interval_ms <= 1000
            or args.seconds * 1000 / args.interval_ms > 60000):
        parser.error("Use bus >= 0, address 0x5a..0x5d, 1..300 seconds, 1..1000 ms and at most 60000 samples")
    # Refuse to overwrite an earlier measurement.
    with args.output.open("x") as output:
        bus = I2CBus(args.bus)
        try:
            initial = read_settings(bus, args.address)
            samples = []
            started = time.monotonic()
            while time.monotonic() - started < args.seconds:
                before = time.monotonic()
                sample = read_sample(bus, args.address)
                sample["t_s"] = round(before - started, 6)
                sample["read_ms"] = round((time.monotonic() - before) * 1000, 3)
                samples.append(sample)
                time.sleep(max(0, args.interval_ms / 1000 - (time.monotonic() - before)))
            final = read_settings(bus, args.address)
        finally:
            bus.close()
        summary = summarize(samples)
        report = {
            "label": args.label, "bus": args.bus, "address": args.address,
            "interval_ms": args.interval_ms, "settings_before": initial,
            "settings_after": final, "settings_changed": initial != final,
            "summary": summary, "samples": samples,
            "limitations": "Baseline registers omit two low bits: displayed delta can be up to 3 counts below the internal delta. Register updates are not atomic. Labels require operator confirmation; no thresholds are inferred automatically.",
        }
        json.dump(report, output)
        output.write("\n")
    print(json.dumps({"output": str(args.output), "settings_changed": initial != final, **summary}, indent=2))


if __name__ == "__main__":
    main()
