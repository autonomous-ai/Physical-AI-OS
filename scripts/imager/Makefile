# =============================================================================
# imager Makefile
# =============================================================================
# Targets:
#   make build                        — OrangePi golden image (default)
#   make TARGET=rpi build             — Raspberry Pi 5 golden image
#   make TARGET=rpi RPI_MODEL=4 build — Raspberry Pi 4B golden image
#   make sd-card-list                 — list disks for flashing
#   make sd-card-test DISK=N          — test SD card with 100 MB dd
#   make sd-card-flash DISK=N         — flash the .xz image to /dev/diskN
#   make sd-card-flash TARGET=rpi DISK=N — flash RPi image instead
#   make sd-card-flash-raw DEVICE_TYPE=lamp DISK=N — flash the raw .img via rdiskN (fast, no wait for .xz)
#   make build ... COMPRESS=0          — skip the .xz step, keep only the raw .img
# =============================================================================

TARGET ?= opi
RPI_MODEL ?= 5
# RPI_MODEL=5 routes to build-pi5.sh (Pi 5 / Trixie fixes: libgpiod3, resolv.conf
# rebuild after STA switch, claude installer multi-path probe, heredoc backtick
# + $OPENCLAW_VERSION escapes). Delete build-pi5.sh and this ternary once the
# fixes are folded back into build.sh.
BUILD_SCRIPT_rpi := $(if $(filter 5,$(RPI_MODEL)),build-pi5.sh,build.sh)
BUILD_SCRIPT_opi := build-orangepi.sh
BUILD_SCRIPT := $(BUILD_SCRIPT_$(TARGET))

# OTA metadata URL must be passed in at build time (no hardcoded default in the
# build scripts). Provide it via env or command line, e.g.:
#   make build OTA_METADATA_URL=https://storage.googleapis.com/<bucket>/<path>/metadata.json
# It is forwarded into the build container and baked into the image's
# /root/config/bootstrap.json. Empty → build fails in preflight.
OTA_METADATA_URL ?=

# Device class baked into this golden image — one image per device type (3 device
# types = 3 golden images). Forwarded into the build container; the build script
# bakes it into os-server.service + /opt/hal/.env and stages only robots/<type>/
# into the image. REQUIRED, no default — a golden image must declare its device
# class. E.g. make build TARGET=opi DEVICE_TYPE=intern.
DEVICE_TYPE ?=

# Per-image default agent runtime, baked into f_r_default_agent so it survives
# a Factory Reset, AND (for DEVICE_TYPE=intern-v2 only) gates SSH — claudecode
# opens it (Developer Edition / black case), every other value closes it (see
# build-orangepi.sh). OPTIONAL — unset behaves exactly like before this
# existed. Also used below to name the output file so multiple builds of the
# SAME DEVICE_TYPE (e.g. intern-v2's blue/orange/black case variants) don't
# clobber each other. There is no separate CASE_COLOR var — the case color is
# purely cosmetic packaging, DEFAULT_AGENT alone drives both the seeded
# runtime and the SSH policy so the two can't disagree.
DEFAULT_AGENT ?=

# Optional hardware assembly from robots/<device>/overrides/<variant>.
# Empty or standard preserves legacy defaults.
VARIANT ?=
VARIANT_SUFFIX := $(if $(filter-out standard,$(VARIANT)),-$(VARIANT),)

# Pi 5 V1 hardware (case đầu) không có loa/mic. Set PI5_NO_AUDIO=1 để build-pi5.sh
# strip audio capability khỏi ROBOT.md — healthwatch skip audio check (LED không
# vàng), HAL skip PortAudio init (không crash log). Chỉ có effect khi
# TARGET=rpi + RPI_MODEL=5. Empty (default) → giữ audio capability như bình thường.
PI5_NO_AUDIO ?=

# CASE_COLOR was removed (see DEFAULT_AGENT above) — fail fast at the Makefile
# level too, so a stale `make build CASE_COLOR=blue` (old muscle memory / an
# un-updated CI script) errors immediately instead of silently building with
# SSH open when the caller expected it closed.
ifneq ($(origin CASE_COLOR),undefined)
$(error CASE_COLOR is removed — set DEFAULT_AGENT=hermes|openclaw|claudecode instead (SSH now follows DEFAULT_AGENT directly))
endif

ifndef BUILD_SCRIPT
$(error Unknown TARGET=$(TARGET). Supported: rpi, opi)
endif

# Image filename per target AND per device type so neither board nor device-type
# builds clobber each other. The build script always writes BUILT_IMG; the build
# target renames it to the device-typed IMG_FILE (3 device types = 3 images).
# OPi build produces .img.xz (compressed); RPi build produces raw .img.
# DEFAULT_AGENT (when set) is folded into IMG_FILE too — named by agent, since
# that's what you actually remember later — so multiple case-color builds of
# the SAME DEVICE_TYPE (e.g. intern-v2 hermes vs. claudecode) land as separate
# files instead of clobbering one another. Unset DEFAULT_AGENT (every build
# before this feature existed) → filename unchanged.
BUILT_IMG_rpi := golden.img
BUILT_IMG_opi := $(DEVICE_TYPE)/golden-opi.img.xz
BUILT_IMG := $(BUILT_IMG_$(TARGET))
IMG_FILE_rpi := golden-$(DEVICE_TYPE)$(if $(DEFAULT_AGENT),-$(DEFAULT_AGENT),)$(VARIANT_SUFFIX).img
IMG_FILE_opi := $(DEVICE_TYPE)/golden-opi-$(DEVICE_TYPE)$(if $(DEFAULT_AGENT),-$(DEFAULT_AGENT),)$(VARIANT_SUFFIX).img.xz
IMG_FILE := $(IMG_FILE_$(TARGET))

# Uncompressed raw image (independent of the .xz). Used by sd-card-flash-raw to flash
# straight from the raw .img — skips waiting for xz compression, and writes via rdiskN.
RAW_IMG_rpi := golden.img
RAW_IMG_opi := $(DEVICE_TYPE)/golden-opi.img
RAW_IMG := $(RAW_IMG_$(TARGET))

# Preflight — check Docker resources + clean dangling layers before a long
# build run. Avoids the most common failure modes: OOM (Docker memory < 4GB)
# and "No space left" / missing .img at xz time (host disk full or Docker
# disk image full). Warns but doesn't block — user can ctrl+C if limits
# look serious. Safe: only prunes dangling images, never volumes or named
# images (UV cache / lerobot clone stay intact).
build-preflight:
	@echo "==> Preflight checks"
	@[ -n "$(OTA_METADATA_URL)" ] || { echo "ERROR: OTA_METADATA_URL is required — e.g. make build OTA_METADATA_URL=https://.../metadata.json"; exit 1; }
	@[ -n "$(DEVICE_TYPE)" ] || { echo "ERROR: DEVICE_TYPE is required — e.g. make build DEVICE_TYPE=lamp"; exit 1; }
	@echo "    OTA metadata URL: $(OTA_METADATA_URL)"
	@echo "    Device type:      $(DEVICE_TYPE)"
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker not installed"; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running (start Docker Desktop)"; exit 1; }
	@DOCKER_MEM_GB=$$(docker info --format '{{.MemTotal}}' | awk '{printf "%.1f", $$1/1024/1024/1024}'); \
		MEM_INT=$$(echo $$DOCKER_MEM_GB | cut -d. -f1); \
		echo "    Docker memory:    $${DOCKER_MEM_GB} GB"; \
		if [ "$$MEM_INT" -lt 4 ]; then \
			echo "    ⚠️  WARNING: <4 GB Docker memory — OOM very likely. Docker Desktop → Settings → Resources → Memory ≥ 8 GB, then restart Docker"; \
		fi
	@FREE_GB=$$(df -k . | tail -1 | awk '{printf "%.0f", $$4/1024/1024}'); \
		echo "    Host free disk:   $${FREE_GB} GB"; \
		if [ "$$FREE_GB" -lt 30 ]; then \
			echo "    ⚠️  WARNING: <30 GB free — image is 14G + workspace ~10G + buffer. Run 'make clean' + free host disk first"; \
		fi
	@echo "    Cleaning dangling Docker images (safe — keeps pi-builder + volumes)"
	@docker image prune -f >/dev/null 2>&1 || true
	@echo

build: build-preflight
	# Clear stale pi-builder image so Docker always rebuilds from current build-orangepi.sh.
	# Prevents the class of bug where a cached layer runs an old script version.
	@docker rmi pi-builder 2>/dev/null || true
	@docker builder prune -af >/dev/null 2>&1 || true
	# Build the Docker build environment (shared for both targets)
	docker build -t pi-builder .

	# Stage the canonical on-device updater as a build input. It is the single
	# source of truth (scripts/provision/software-update) shared with setup.sh;
	# the Docker build context is this directory, so the file cannot be COPY'd
	# into the builder image — it rides the /input mount instead. Re-copied on
	# every build, so the staged copy can never go stale.
	cp ../provision/software-update input/software-update

	# Run it — needs --privileged for losetup/mount.
	# BUILD_SCRIPT controls which builder runs inside the container.
	docker run --rm \
	--privileged \
	-v ./output:/output \
	-v ./input:/input \
	-e BUILD_SCRIPT=$(BUILD_SCRIPT) \
	-e RPI_MODEL=$(RPI_MODEL) \
	-e OTA_METADATA_URL=$(OTA_METADATA_URL) \
	-e DEVICE_TYPE=$(DEVICE_TYPE) \
	-e DEFAULT_AGENT=$(DEFAULT_AGENT) \
	-e VARIANT=$(VARIANT) \
	-e PI5_NO_AUDIO=$(PI5_NO_AUDIO) \
	-e COMPRESS=$(COMPRESS) \
	-e BUILD_GIT_SHA=$(shell git -C .. rev-parse HEAD 2>/dev/null || echo unknown) \
	pi-builder /work/$(BUILD_SCRIPT)
	@# Name the output per device type so lamp/intern/dog images don't clobber.
	@if [ "$(BUILT_IMG)" != "$(IMG_FILE)" ] && [ -f output/$(BUILT_IMG) ]; then mv -f output/$(BUILT_IMG) output/$(IMG_FILE); fi
	@echo "==> Built image: output/$(IMG_FILE)  (device_type=$(DEVICE_TYPE))"

sd-card-list:
	diskutil list

sd-card-test:
ifndef DISK
	$(error DISK is required. Usage: make sd-card-test DISK=19)
endif
	@echo "==> Testing SD card write (100 MB of zeros)..."
	sudo launchctl unload /System/Library/LaunchDaemons/com.apple.diskarbitrationd.plist 2>/dev/null || true
	diskutil unmountDisk /dev/disk$(DISK) 2>/dev/null || true
	sleep 1
	sudo dd if=/dev/zero of=/dev/rdisk$(DISK) bs=1m count=1000 status=progress; \
	DD_EXIT=$$?; \
	sudo launchctl load /System/Library/LaunchDaemons/com.apple.diskarbitrationd.plist 2>/dev/null || true; \
	if [ $$DD_EXIT -eq 0 ]; then echo "==> SD card OK — hardware is fine"; else echo "==> SD card FAILED — hardware issue (bad card or reader)"; fi; \
	exit $$DD_EXIT

sd-card-flash:
ifndef DISK
	$(error DISK is required. Usage: make sd-card-flash DISK=4)
endif
	@command -v python3 >/dev/null
	@[ -s ./output/$(IMG_FILE) ] || { echo "ERROR: image missing or empty"; exit 1; }
	@echo "==> Flashing ./output/$(IMG_FILE) to /dev/disk$(DISK)..."
	diskutil unmountDisk force /dev/disk$(DISK)
	@# Compressed (.xz) images decompress on the fly; raw .img dd directly.
	@bash -eu -o pipefail -c 'if [ "$(suffix $(IMG_FILE))" = ".xz" ]; then \
		xz -dc ./output/$(IMG_FILE) | sudo dd of=/dev/disk$(DISK) bs=64m conv=fsync status=progress; \
	else \
		sudo dd if=./output/$(IMG_FILE) of=/dev/disk$(DISK) bs=64m conv=fsync status=progress; \
	fi'
	sync
	diskutil unmountDisk /dev/disk$(DISK)
	sudo "$$(command -v python3)" lib/verify_flash.py ./output/$(IMG_FILE) /dev/rdisk$(DISK)
	diskutil eject /dev/disk$(DISK) 2>/dev/null || true
	@echo "==> Flash complete"

# Flash the RAW uncompressed image straight to the card. For dev machines that already
# have the raw output/<type>/golden-opi.img — skips waiting for the .xz (which exists
# only to compress for CDN upload). Writes via /dev/rdiskN (raw, unbuffered = 2-5x
# faster than the buffered /dev/diskN on macOS).
#   make sd-card-flash-raw DEVICE_TYPE=lamp DISK=12
sd-card-flash-raw:
ifndef DISK
	$(error DISK is required. Usage: make sd-card-flash-raw DEVICE_TYPE=lamp DISK=12)
endif
ifneq ($(VERIFY),0)
	@command -v python3 >/dev/null
endif
	@[ -s ./output/$(RAW_IMG) ] || { echo "ERROR: ./output/$(RAW_IMG) not found — pass DEVICE_TYPE, or the raw .img isn't built yet"; exit 1; }
	@echo "==> Flashing ./output/$(RAW_IMG) to /dev/rdisk$(DISK) (raw, fast)..."
	diskutil unmountDisk force /dev/disk$(DISK)
	sudo dd if=./output/$(RAW_IMG) of=/dev/rdisk$(DISK) bs=64m conv=fsync status=progress
	sync
	diskutil unmountDisk /dev/disk$(DISK)
ifneq ($(VERIFY),0)
	sudo "$$(command -v python3)" lib/verify_flash.py ./output/$(RAW_IMG) /dev/rdisk$(DISK)
else
	@echo "==> Readback verification skipped (VERIFY=0)"
endif
	diskutil eject /dev/disk$(DISK) 2>/dev/null || true
	@echo "==> Flash complete"

# ── upload — push built image + release note to GCS, version-stamped ─────
# Versioned filename: golden-<target>-<UTC-timestamp>-<git-sha>.img.xz
# Bucket + path mirror the OTA artifact layout (s3-autonomous-upgrade-3/os/).
GCS_BUCKET := s3-autonomous-upgrade-3
GCS_PATH   := os/imager/output
GCS_LEDGER := os/imager/RELEASES.md
VERSION    := $(shell date -u +%Y%m%d-%H%M%S)-$(shell git -C .. rev-parse --short HEAD 2>/dev/null || echo nogit)
TARGET_PREFIX_rpi := golden-rpi
TARGET_PREFIX_opi := golden-opi
TARGET_PREFIX := $(TARGET_PREFIX_$(TARGET))
VERSIONED_BASE := $(TARGET_PREFIX)-$(DEVICE_TYPE)$(VARIANT_SUFFIX)-$(VERSION)
VERSIONED_IMG  := $(VERSIONED_BASE)$(if $(filter %.xz,$(IMG_FILE)),.img.xz,.img)
RELEASE_NOTE   := output/$(DEVICE_TYPE)/$(VERSIONED_BASE).release.txt
MANIFEST_FILE  := output/$(DEVICE_TYPE)/manifest-$(TARGET).json

upload: $(RELEASE_NOTE)
	@command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud not installed. brew install --cask gcloud-cli"; exit 1; }
	@echo "==> Uploading image → gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)"
	gcloud storage cp output/$(IMG_FILE) gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)
	@echo "==> Uploading release note → gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_BASE).release.txt"
	gcloud storage cp $(RELEASE_NOTE) gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_BASE).release.txt
	@echo "==> Updating cumulative ledger gs://$(GCS_BUCKET)/$(GCS_LEDGER)"
	@TMP=$$(mktemp); \
	if gcloud storage cp gs://$(GCS_BUCKET)/$(GCS_LEDGER) $$TMP 2>/dev/null; then \
		echo "    (existing ledger fetched, prepending new entry)"; \
	else \
		printf "# Lamp Imager Releases\n\nAppend-only history of built images. Newest first.\n\n" > $$TMP; \
	fi; \
	NEW=$$(mktemp); \
	{ awk '/^# Lamp Imager Releases/,/^$$/' $$TMP; cat $(RELEASE_NOTE); echo ""; echo "---"; echo ""; awk 'BEGIN{p=0} /^## /{p=1} p' $$TMP; } > $$NEW; \
	gcloud storage cp $$NEW gs://$(GCS_BUCKET)/$(GCS_LEDGER); \
	rm -f $$TMP $$NEW
	@echo ""
	@echo "==> DONE. Public URLs:"
	@echo "    Image:   https://storage.googleapis.com/$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)"
	@echo "    Release: https://storage.googleapis.com/$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_BASE).release.txt"
	@echo "    Ledger:  https://storage.googleapis.com/$(GCS_BUCKET)/$(GCS_LEDGER)"
	@echo ""
	@echo "==> Cleaning output/ (set KEEP_OUTPUT=1 to skip)"
	@if [ -z "$$KEEP_OUTPUT" ]; then \
		rm -f output/$(IMG_FILE) $(RELEASE_NOTE) $(MANIFEST_FILE); \
		echo "    Removed: output/$(IMG_FILE)"; \
		echo "    Removed: $(RELEASE_NOTE)"; \
		echo "    Removed: $(MANIFEST_FILE)"; \
	else \
		echo "    KEEP_OUTPUT set — leaving output/ alone"; \
	fi

# Release note generated from manifest + image checksums at upload time.
$(RELEASE_NOTE):
	@[ -f output/$(IMG_FILE) ] || { echo "ERROR: output/$(IMG_FILE) not found. Run 'make build' first."; exit 1; }
	@mkdir -p output
	@echo "==> Generating release note $(RELEASE_NOTE)"
	@IMG_SHA=$$(shasum -a 256 output/$(IMG_FILE) | awk '{print $$1}'); \
	IMG_BYTES=$$(stat -f %z output/$(IMG_FILE) 2>/dev/null || stat -c %s output/$(IMG_FILE)); \
	IMG_SIZE=$$(du -h output/$(IMG_FILE) | awk '{print $$1}'); \
	GIT_SHA_LONG=$$(git -C .. rev-parse HEAD 2>/dev/null || echo unknown); \
	GIT_BRANCH=$$(git -C .. rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown); \
	BUILT_AT=$$(date -u +%Y-%m-%dT%H:%M:%SZ); \
	{ \
		echo "## $(VERSIONED_BASE)"; \
		echo ""; \
		echo "- Target:        $(TARGET)"; \
		echo "- Built (UTC):   $$BUILT_AT"; \
		echo "- Git branch:    $$GIT_BRANCH"; \
		echo "- Git commit:    $$GIT_SHA_LONG"; \
		echo "- Image size:    $$IMG_SIZE ($$IMG_BYTES bytes)"; \
		echo "- sha256:        $$IMG_SHA"; \
		echo "- Output name:   $(VERSIONED_IMG)"; \
		echo "- Download:      https://storage.googleapis.com/$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)"; \
		if [ -f $(MANIFEST_FILE) ]; then \
			echo ""; \
			echo "OTA versions baked at build time:"; \
			jq -r '.ota_versions | to_entries[] | "- \(.key): \(.value)"' $(MANIFEST_FILE) 2>/dev/null || echo "(could not parse manifest)"; \
			echo ""; \
			echo "Source image:"; \
			jq -r '.source_image | "- file_id: \(.file_id)\n- name:    \(.name)\n- sha256:  \(.sha256)"' $(MANIFEST_FILE) 2>/dev/null || true; \
			echo ""; \
			echo "Build config:"; \
			jq -r '"- openclaw: \(.openclaw_version)\n- out_size: \(.out_img_size)\n- ota_url:  \(.ota_metadata_url)"' $(MANIFEST_FILE) 2>/dev/null || true; \
		else \
			echo ""; \
			echo "(no $(MANIFEST_FILE) — OTA versions not captured this build)"; \
		fi; \
	} > $(RELEASE_NOTE)
	@echo "==> Release note written:"
	@cat $(RELEASE_NOTE)

OPI_FILE_NAME := Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z
GCS_BASE_PATH := os/imager/base

# Per-device base image local paths + GCS destination filenames.
# Differentiated by directory (same filename golden-opi-dev.img.xz, different GCS key).
_BASE_LOCAL_lamp      := input/lamp/golden-opi-dev.img.xz
_BASE_LOCAL_intern-v2 := input/intern-v2/golden-opi-dev.img.xz
_BASE_GCS_lamp        := golden-opi-dev-lamp.img.xz
_BASE_GCS_intern-v2   := golden-opi-dev-intern-v2.img.xz

# Resolve to stock .7z when DEVICE_TYPE is empty.
_UPLOAD_LOCAL := $(if $(DEVICE_TYPE),$(_BASE_LOCAL_$(DEVICE_TYPE)),input/orangepi.7z)
_UPLOAD_GCS   := $(if $(DEVICE_TYPE),$(GCS_BASE_PATH)/$(_BASE_GCS_$(DEVICE_TYPE)),os/imager/source/$(OPI_FILE_NAME))

# Upload stock .7z OR a per-device pre-built base image to GCS.
#   make upload-source                    — stock OrangePi vendor .7z
#   make upload-source DEVICE_TYPE=lamp   — hardware team's lamp base image
#   make upload-source DEVICE_TYPE=intern-v2
upload-source:
	@command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud not installed. brew install --cask gcloud-cli"; exit 1; }
	@[ -n "$(_UPLOAD_LOCAL)" ] || { echo "ERROR: unknown DEVICE_TYPE=$(DEVICE_TYPE). Supported: lamp, intern-v2"; exit 1; }
	@[ -f "$(_UPLOAD_LOCAL)" ] || { echo "ERROR: $(_UPLOAD_LOCAL) not found"; exit 1; }
	@echo "==> Uploading $(_UPLOAD_LOCAL) → gs://$(GCS_BUCKET)/$(_UPLOAD_GCS)"
	gcloud storage cp $(_UPLOAD_LOCAL) gs://$(GCS_BUCKET)/$(_UPLOAD_GCS)
	@echo "==> Public URL:"
	@echo "    https://storage.googleapis.com/$(GCS_BUCKET)/$(_UPLOAD_GCS)"

# ── housekeeping ───────────────────────────────────────────────────────────
# `make clean` wipes output/ entirely (built images, release notes, manifest,
# any leftover raw .img). Use after a successful upload to free disk space.
# Does NOT touch input/ (cached source images stay).
clean:
	@echo "==> Wiping output/"
	rm -rf output/*
	@echo "    (input/ kept — set 'rm -rf input/*' manually to force fresh source download)"

clean-all: clean
	@echo "==> Wiping input/ too"
	rm -rf input/*
