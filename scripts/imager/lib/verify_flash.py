#!/usr/bin/env python3
"""Read back exactly the image bytes from a flashed disk; never write the disk."""

import argparse
import lzma
from pathlib import Path


def verify(image: Path, device: Path):
    opener = lzma.open if image.suffix == '.xz' else open
    offset = 0
    with opener(image, 'rb') as source, device.open('rb', buffering=0) as target:
        while chunk := source.read(8 * 1024 * 1024):
            actual = bytearray()
            while len(actual) < len(chunk):
                part = target.read(len(chunk) - len(actual))
                if not part:
                    raise ValueError(f'Disk ended at byte {offset + len(actual)}')
                actual.extend(part)
            if actual != chunk:
                first = next(i for i, (a, b) in enumerate(zip(actual, chunk)) if a != b)
                raise ValueError(f'Readback mismatch at byte {offset + first}')
            offset += len(chunk)
    if not offset:
        raise ValueError('Image is empty')
    print(f'Verified {offset} bytes: disk matches image')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('device', type=Path)
    args = parser.parse_args()
    verify(args.image, args.device)
