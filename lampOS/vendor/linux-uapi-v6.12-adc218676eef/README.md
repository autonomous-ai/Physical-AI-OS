# Pinned Linux media userspace headers

This is a build-only subset of the Linux v6.12 userspace API, pinned to commit
`adc218676eef25575469234709c2d87185ca223a`. It contains the userspace-exported
`videodev2.h`, `v4l2-common.h` and `v4l2-controls.h` from that exact source.
`UPSTREAM.json` records the source, export-tool and output SHA256 values.
`SHA256SUMS` covers all four shipped headers and the three license texts.

The three upstream headers total 255,190 bytes. They supply the AV1 declarations
unconditionally referenced by pinned `v4l2r 0.0.8`, even though Lamp's camera
backend only uses single-planar MJPG capture. No AV1 operation is enabled by
bundling these definitions. This directory does not replace system headers,
install a kernel, or add a runtime service or shared-library dependency.

## License and provenance

The three upstream headers retain their original copyright notices and SPDX
expression: `(GPL-2.0+ WITH Linux-syscall-note) OR BSD-3-Clause`. This distribution
uses their BSD-3-Clause alternative and includes every referenced license text
under `LICENSES/`. Retain these notices and license texts in source and binary
redistributions. The top-level forwarding
header contains only an include directive; it adds no copied declarations or
different license terms.

Authoritative sources:

- [Pinned Linux source](https://github.com/torvalds/linux/tree/adc218676eef25575469234709c2d87185ca223a/include/uapi/linux)
- [Official userspace header export](https://www.kernel.org/doc/html/latest/kbuild/headers_install.html)
- [Linux licensing rules](https://www.kernel.org/doc/html/latest/process/license-rules.html)

## Build selection and checks

The workspace `.cargo/config.toml` sets `V4L2R_VIDEODEV2_H_PATH` to this
`include` directory using Cargo's `relative = true` and `force = true` options.
Run Cargo from the lampOS root or a descendant; `--manifest-path` from an
unrelated working directory does not load the workspace's Cargo configuration.

Upstream v4l2r adds this directory with `-I` but its wrapper includes
`<linux/videodev2.h>`. It separately watches a top-level `videodev2.h` file for
rebuilds. The regular forwarding header supplies that watch path without a
symlink or a second copy of the declarations. `lamp-camera/build.rs` verifies
the selected canonical directory, exact lengths and SHA256 of all four headers
on every build-script run. Missing, changed or redirected bundle inputs fail the
build. The check uses the already-pinned Rust `sha2` dependency; it performs no
network access and does not generate bindings or open a device.

Base ABI definitions remain explicit target toolchain dependencies:
`sys/time.h`, `linux/ioctl.h`, `linux/types.h`, `linux/const.h`, and their target
`asm`/`asm-generic` and libc includes. Normal builds also need libclang 9 or
newer and its resource headers for bindgen. The `clang` executable itself is not
required by ordinary bindgen generation, but is useful for include auditing.
Record the actual compiler, libclang, libc and base-UAPI package versions for
native qualification. Keep header-overriding `BINDGEN_EXTRA_CLANG_ARGS` and
global include-path workarounds unset. No extra clang flags are required here.
Leave v4l2r's `arch32`/`arch64` features disabled on ARM64 because those features
select x86 targets.

This media subset is not a complete cross-compilation sysroot. A newer header
bundle does not establish older-kernel compatibility by itself. Before hardware
use, compile/test the Linux adapter and compare the existing capture ioctl
numbers and relevant struct layouts against the installed UAPI. Camera driver
behavior, negotiated modes and privacy timing require separate physical tests.

## Why keep the reviewed binding

Published v4l2r 0.0.6 has no AV1 payload wrappers, but still requires libclang
through bindgen 0.70.1. It lacks the current REQBUFS `MemoryConsistency` argument
and predates corrected cache-flag names and a multiplanar QBUF length fix.
Downgrading would require camera call-site changes and a renewed binding audit.
v4l2r 0.0.7 already references AV1, so it does not remove the header requirement.
Version 0.0.8 has no AV1-off feature or pre-generated-bindings build option.
`UPSTREAM.json` records checksums and commits of the examined published archives.
These are source comparisons, not successful native builds of those alternatives.

## Reproduce the exported bytes

Generation is separate from ordinary Cargo builds. Obtain the exact pinned
Linux sources listed in `UPSTREAM.json`, verify their hashes, and work in a
scratch tree with the original relative paths. Compile that tree's
`scripts/unifdef.c` to `scripts/unifdef`; use GNU sed on PATH and locale `C`.
The recorded export used GNU sed 4.9. macOS BSD sed does not accept all syntax
in the unmodified upstream script. No export script or source edits are needed.

```sh
cc -O2 -o scripts/unifdef scripts/unifdef.c
mkdir -p export/include/linux
LC_ALL=C sh scripts/headers_install.sh include/uapi/linux/videodev2.h export/include/linux/videodev2.h
LC_ALL=C sh scripts/headers_install.sh include/uapi/linux/v4l2-common.h export/include/linux/v4l2-common.h
LC_ALL=C sh scripts/headers_install.sh include/uapi/linux/v4l2-controls.h export/include/linux/v4l2-controls.h
```

The script removes kernel-only branches, compiler includes and annotations;
raw source headers are not interchangeable with the checked-in export. Compare
the output hashes with `UPSTREAM.json` before copying. Export tools are not
needed by normal builds and are not shipped as Lamp runtime components.
Verify this directory independently with `sha256sum -c SHA256SUMS` (Linux) or
`shasum -a 256 -c SHA256SUMS` (macOS).

Treat this versioned directory as immutable. A reviewed upstream update uses a
new version/commit directory and updates the forced Cargo path, build-time
length/digest constants and provenance together. Changing the selected path
also makes upstream v4l2r regenerate its bindings. Do not update only an included
header in place: upstream's build script does not watch all transitive includes.
