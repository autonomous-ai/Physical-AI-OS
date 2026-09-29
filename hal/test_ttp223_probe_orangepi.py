#!/usr/bin/env python3
"""Standalone TTP223 pad probe (stdlib ioctl only; no gpiod, no sudo).

info [lines...]: passive line dump, safe while hal.service runs.
watch [lines...]: claim lines and print level changes (stop hal first).
Example: python3 hal/test_ttp223_probe_orangepi.py info --device-type lamp --devices-dir /opt/devices
"""

from __future__ import annotations

import argparse
from pathlib import Path

import fcntl
import os
import struct
import sys
import time

# GPIO uAPI v2, from linux/gpio.h
GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

# struct gpio_v2_line_attribute { __u32 id; __u32 padding; union {...} u64; }
SIZEOF_ATTR = 16
# struct gpio_v2_line_config_attribute { attr; __aligned_u64 mask; }
SIZEOF_CFG_ATTR = SIZEOF_ATTR + 8
# struct gpio_v2_line_config { u64 flags; u32 num_attrs; u32 padding[5]; attrs[10]; }
SIZEOF_CONFIG = 8 + 4 + 20 + GPIO_V2_LINE_NUM_ATTRS_MAX * SIZEOF_CFG_ATTR
# struct gpio_v2_line_request { u32 offsets[64]; char consumer[32]; config;
#                               u32 num_lines; u32 event_buffer_size;
#                               u32 padding[5]; s32 fd; }
SIZEOF_REQUEST = (
    GPIO_V2_LINES_MAX * 4 + GPIO_MAX_NAME_SIZE + SIZEOF_CONFIG + 4 + 4 + 20 + 4
)
# struct gpio_v2_line_info { char name[32]; char consumer[32]; u32 offset;
#                            u32 num_attrs; u64 flags; attrs[10]; u32 padding[4]; }
SIZEOF_LINE_INFO = 32 + 32 + 4 + 4 + 8 + GPIO_V2_LINE_NUM_ATTRS_MAX * SIZEOF_ATTR + 16
# struct gpio_v2_line_values { __aligned_u64 bits; __aligned_u64 mask; }
SIZEOF_VALUES = 16

assert SIZEOF_CONFIG == 272, SIZEOF_CONFIG
assert SIZEOF_REQUEST == 592, SIZEOF_REQUEST
assert SIZEOF_LINE_INFO == 256, SIZEOF_LINE_INFO


def _iowr(type_: int, nr: int, size: int) -> int:
    """_IOWR from asm-generic/ioctl.h: dir=3 (read|write)."""
    return (3 << 30) | (size << 16) | (type_ << 8) | nr


GPIO_V2_GET_LINEINFO_IOCTL = _iowr(0xB4, 0x05, SIZEOF_LINE_INFO)
GPIO_V2_GET_LINE_IOCTL = _iowr(0xB4, 0x07, SIZEOF_REQUEST)
GPIO_V2_LINE_GET_VALUES_IOCTL = _iowr(0xB4, 0x0E, SIZEOF_VALUES)

FLAG_INPUT = 1 << 2
FLAG_BIAS_PULL_UP = 1 << 8

_FLAG_NAMES = [
    (1 << 0, "USED"),
    (1 << 1, "ACTIVE_LOW"),
    (1 << 2, "INPUT"),
    (1 << 3, "OUTPUT"),
    (1 << 4, "EDGE_RISING"),
    (1 << 5, "EDGE_FALLING"),
    (1 << 6, "OPEN_DRAIN"),
    (1 << 7, "OPEN_SOURCE"),
    (1 << 8, "PULL_UP"),
    (1 << 9, "PULL_DOWN"),
    (1 << 10, "BIAS_DISABLED"),
]


def _decode_flags(flags: int) -> str:
    on = [name for bit, name in _FLAG_NAMES if flags & bit]
    return ",".join(on) if on else "-"


def _wiring(device_type: str, devices_dir: str) -> tuple[int, list[int]]:
    """Resolve the same device override and board fallback as HAL."""
    from hal.board.board import board_profile
    from hal.board.ttp223 import load_touch_config

    touch = load_touch_config(str(Path(devices_dir) / device_type), board_profile().id)
    if touch is None:
        sys.exit(f"device {device_type!r} has no active TTP223 wiring on this board")
    return touch.chip, list(touch.lines)


def cmd_info(chip: int, lines: list[int]) -> None:
    """Passive dump. Safe to run with hal.service up — claims nothing."""
    path = f"/dev/gpiochip{chip}"
    print(f"{path}  (passive read — hal.service may keep running)\n")
    print(f"{'line':>5}  {'consumer':<16} flags")
    with open(path, "rb") as f:
        for line in lines:
            buf = bytearray(SIZEOF_LINE_INFO)
            struct.pack_into("<I", buf, 64, line)  # .offset, after name+consumer
            try:
                fcntl.ioctl(f.fileno(), GPIO_V2_GET_LINEINFO_IOCTL, buf, True)
            except OSError as e:
                print(f"{line:>5}  <ioctl failed: {e}>")
                continue
            consumer = bytes(buf[32:64]).split(b"\x00")[0].decode() or "-"
            flags = struct.unpack_from("<Q", buf, 72)[0]
            print(f"{line:>5}  {consumer:<16} {_decode_flags(flags)}")
    print(
        "\nA line held by consumer 'lg' is one HAL claimed. A free line that "
        "should be a pad\nmeans the selected wiring configuration and the hardware disagree."
    )


def cmd_watch(chip: int, lines: list[int]) -> None:
    """Claim the lines pulled up and print level changes. Needs hal stopped."""
    path = f"/dev/gpiochip{chip}"
    req = bytearray(SIZEOF_REQUEST)
    for i, line in enumerate(lines):
        struct.pack_into("<I", req, i * 4, line)
    struct.pack_into("<32s", req, 256, b"ttp223-probe")
    # config.flags at offset 256+32
    struct.pack_into("<Q", req, 288, FLAG_INPUT | FLAG_BIAS_PULL_UP)
    # num_lines follows the config block
    struct.pack_into("<I", req, 256 + 32 + SIZEOF_CONFIG, len(lines))

    with open(path, "rb") as f:
        try:
            fcntl.ioctl(f.fileno(), GPIO_V2_GET_LINE_IOCTL, req, True)
        except OSError as e:
            sys.exit(
                f"claim failed: {e}\n"
                "If this is EBUSY, hal.service still holds these lines:\n"
                "    sudo systemctl stop hal"
            )
        req_fd = struct.unpack_from("<i", req, SIZEOF_REQUEST - 4)[0]

    mask = (1 << len(lines)) - 1
    print(f"watching {lines} on {path} (PULL_UP). Touch one pad at a time. Ctrl+C to exit.")
    print("pads rest HIGH — a touch reads LOW.\n")
    last: dict[int, int | None] = {l: None for l in lines}
    try:
        while True:
            vals = struct.pack("<QQ", 0, mask)
            buf = bytearray(vals)
            fcntl.ioctl(req_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, buf, True)
            bits = struct.unpack_from("<Q", buf, 0)[0]
            for i, line in enumerate(lines):
                v = (bits >> i) & 1
                if last[line] != v:
                    stamp = time.strftime("%H:%M:%S")
                    print(f"{stamp}  line {line} = {'HIGH' if v else 'LOW (touch)'}")
                    last[line] = v
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        os.close(req_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("info", "watch"), nargs="?", default="info")
    parser.add_argument("lines", nargs="*", type=int)
    parser.add_argument("--device-type", default=os.environ.get("DEVICE_TYPE"))
    parser.add_argument("--devices-dir", default=os.environ.get("DEVICES_DIR") or
                        str(Path(__file__).resolve().parents[1] / "robots"))
    args = parser.parse_args()
    if not args.device_type:
        parser.error("--device-type or DEVICE_TYPE is required to select device wiring")
    chip, lines = _wiring(args.device_type, args.devices_dir)
    if args.lines:
        lines = args.lines
    cmd_info(chip, lines) if args.mode == "info" else cmd_watch(chip, lines)



if __name__ == "__main__":
    main()
